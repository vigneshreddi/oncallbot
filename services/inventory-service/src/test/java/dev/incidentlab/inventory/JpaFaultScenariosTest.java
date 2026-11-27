package dev.incidentlab.inventory;

import static org.assertj.core.api.Assertions.*;

import org.hibernate.LazyInitializationException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.domain.EntityScan;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.runner.WebApplicationContextRunner;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.context.annotation.Import;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.server.ResponseStatusException;

@SpringBootTest(classes = JpaFaultScenariosTest.TestApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {
      "spring.datasource.url=jdbc:h2:mem:jpa_faults;MODE=PostgreSQL;DB_CLOSE_DELAY=-1;INIT=CREATE SCHEMA IF NOT EXISTS inventory",
      "spring.datasource.username=sa", "spring.datasource.password=",
      "inventory.demo-faults.enabled=true"
    })
@ActiveProfiles("demo")
@ExtendWith(OutputCaptureExtension.class)
class JpaFaultScenariosTest {
  @SpringBootConfiguration
  @EnableAutoConfiguration
  @EntityScan(basePackageClasses = JpaFaultBatch.class)
  @EnableJpaRepositories(basePackageClasses = JpaFaultBatchRepository.class)
  @Import({JpaFaultScenarios.class, JpaFaultController.class})
  static class TestApplication {}

  @Autowired JpaFaultScenarios scenarios;
  @Autowired JdbcTemplate jdbc;
  @Autowired TestRestTemplate http;

  @AfterEach
  void cleanup() {
    scenarios.cleanup();
  }

  @Test
  void lazyCollectionFailsOnlyAfterTransactionCloses() {
    scenarios.prepare();
    assertThatThrownBy(scenarios::provokeLazyLoading).isInstanceOf(LazyInitializationException.class);
    assertThat(jdbc.queryForObject("select count(*) from inventory.jpa_fault_reservation", Integer.class)).isEqualTo(1);
  }

  @Test
  void competingCommitWinsAndStaleTransactionRollsBack() {
    scenarios.prepare();
    for (int attempt = 1; attempt <= 3; attempt++) {
      assertThatThrownBy(scenarios::provokeOptimisticLock).isInstanceOf(OptimisticLockingFailureException.class);
      assertThat(jdbc.queryForObject("select version from inventory.jpa_fault_batch where id=1", Long.class)).isEqualTo((long) attempt);
      assertThat(jdbc.queryForObject("select reserved_qty from inventory.jpa_fault_batch where id=1", Integer.class)).isEqualTo(attempt);
    }
  }

  @Test
  void httpWorkflowsReturnRealFailuresAndCleanupIsIdempotent(CapturedOutput output) {
    assertThat(http.postForEntity("/inventory/admin/jpa/prepare", null, String.class).getStatusCode().value()).isEqualTo(200);
    assertThat(http.postForEntity("/inventory/admin/jpa/lazy/provoke", null, String.class).getStatusCode().value()).isEqualTo(500);
    assertThat(http.postForEntity("/inventory/admin/jpa/optimistic-lock/provoke", null, String.class).getStatusCode().value()).isEqualTo(500);
    assertThat(output.getAll()).contains("JPA demo fault jpa-lazy", "JPA demo fault jpa-optimistic-lock");
    for (int attempt = 0; attempt < 2; attempt++) {
      assertThat(http.postForEntity("/inventory/admin/jpa/cleanup", null, String.class).getStatusCode().value()).isEqualTo(200);
    }
    assertThat(jdbc.queryForObject("select count(*) from inventory.jpa_fault_batch", Integer.class)).isZero();
    assertThat(jdbc.queryForObject("select count(*) from inventory.jpa_fault_reservation", Integer.class)).isZero();
    assertThatThrownBy(scenarios::provokeOptimisticLock).isInstanceOf(ResponseStatusException.class);
  }

  @Test
  void prepareReplacesOnlyTheFixedFixture() {
    scenarios.prepare();
    jdbc.update("insert into inventory.jpa_fault_batch(id, version, reserved_qty) values (99, 0, 7)");
    try {
      scenarios.prepare();
      scenarios.cleanup();
      assertThat(jdbc.queryForObject("select reserved_qty from inventory.jpa_fault_batch where id=99", Integer.class)).isEqualTo(7);
    } finally {
      jdbc.update("delete from inventory.jpa_fault_batch where id=99");
    }
  }

  @Test
  void gatesRequireBothDemoProfileAndExplicitOptIn() {
    WebApplicationContextRunner runner = new WebApplicationContextRunner()
        .withUserConfiguration(JpaFaultScenarios.class, JpaFaultController.class);
    runner.run(context -> assertThat(context).doesNotHaveBean(JpaFaultController.class).doesNotHaveBean(JpaFaultScenarios.class));
    runner.withPropertyValues("spring.profiles.active=demo").run(context ->
        assertThat(context).doesNotHaveBean(JpaFaultController.class).doesNotHaveBean(JpaFaultScenarios.class));
    runner.withPropertyValues("inventory.demo-faults.enabled=true").run(context ->
        assertThat(context).doesNotHaveBean(JpaFaultController.class).doesNotHaveBean(JpaFaultScenarios.class));
  }
}