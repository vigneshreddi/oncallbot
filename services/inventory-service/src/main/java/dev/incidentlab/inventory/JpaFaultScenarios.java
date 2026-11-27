package dev.incidentlab.inventory;

import jakarta.annotation.PostConstruct;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.server.ResponseStatusException;

@Service
@Profile("demo")
@ConditionalOnProperty(name = "inventory.demo-faults.enabled", havingValue = "true")
public class JpaFaultScenarios {
  private static final Logger log = LoggerFactory.getLogger(JpaFaultScenarios.class);
  private static final long FIXTURE_ID = 1L;
  private final JdbcTemplate jdbc;
  private final JpaFaultBatchRepository batches;
  private final TransactionTemplate transaction;
  private final TransactionTemplate readTransaction;
  private final TransactionTemplate competingTransaction;

  public JpaFaultScenarios(JdbcTemplate jdbc, JpaFaultBatchRepository batches,
                          PlatformTransactionManager manager) {
    this.jdbc = jdbc;
    this.batches = batches;
    transaction = new TransactionTemplate(manager);
    transaction.setTimeout(5);
    readTransaction = new TransactionTemplate(manager);
    readTransaction.setReadOnly(true);
    readTransaction.setTimeout(5);
    competingTransaction = new TransactionTemplate(manager);
    competingTransaction.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    competingTransaction.setTimeout(5);
  }

  @PostConstruct
  public void initializeFixtures() {
    jdbc.execute("""
        create table if not exists inventory.jpa_fault_batch (
          id bigint primary key,
          version bigint not null,
          reserved_qty integer not null,
          created_at timestamp with time zone not null default now(),
          updated_at timestamp with time zone not null default now())""");
    jdbc.execute("""
        create table if not exists inventory.jpa_fault_reservation (
          id bigint primary key,
          batch_id bigint not null references inventory.jpa_fault_batch(id),
          created_at timestamp with time zone not null default now(),
          updated_at timestamp with time zone not null default now())""");
  }

  public synchronized Map<String, Object> prepare() {
    transaction.executeWithoutResult(status -> {
      deleteFixture();
      batches.saveAndFlush(new JpaFaultBatch(FIXTURE_ID));
    });
    return Map.of("fixtureId", FIXTURE_ID, "prepared", true);
  }

  public synchronized int provokeLazyLoading() {
    JpaFaultBatch detached = readTransaction.execute(status -> fixture());
    log.error("JPA demo fault jpa-lazy: accessing detached reservations for fixtureId={}", FIXTURE_ID);
    return detached.getReservations().size();
  }

  public synchronized void provokeOptimisticLock() {
    transaction.executeWithoutResult(status -> {
      JpaFaultBatch stale = fixture();
      competingTransaction.executeWithoutResult(competingStatus -> {
        fixture().reserveOne();
        batches.flush();
      });
      stale.reserveOne();
      log.error("JPA demo fault jpa-optimistic-lock: flushing stale version for fixtureId={}", FIXTURE_ID);
      batches.flush();
    });
  }

  public synchronized Map<String, Object> cleanup() {
    transaction.executeWithoutResult(status -> deleteFixture());
    return Map.of("fixtureId", FIXTURE_ID, "cleaned", true);
  }

  private JpaFaultBatch fixture() {
    return batches.findById(FIXTURE_ID).orElseThrow(() ->
        new ResponseStatusException(HttpStatus.CONFLICT, "Prepare the JPA fixture first"));
  }

  private void deleteFixture() {
    jdbc.update("delete from inventory.jpa_fault_reservation where batch_id = ?", FIXTURE_ID);
    jdbc.update("delete from inventory.jpa_fault_batch where id = ?", FIXTURE_ID);
  }
}