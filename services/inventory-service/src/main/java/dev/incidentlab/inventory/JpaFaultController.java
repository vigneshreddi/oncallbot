package dev.incidentlab.inventory;

import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.*;

@RestController
@Profile("demo")
@ConditionalOnProperty(name = "inventory.demo-faults.enabled", havingValue = "true")
@RequestMapping("/inventory/admin/jpa")
public class JpaFaultController {
  private final JpaFaultScenarios scenarios;

  public JpaFaultController(JpaFaultScenarios scenarios) {
    this.scenarios = scenarios;
  }

  @PostMapping("/prepare")
  public Map<String, Object> prepare() {
    return scenarios.prepare();
  }

  @PostMapping("/lazy/provoke")
  public Map<String, Object> lazy() {
    return Map.of("reservations", scenarios.provokeLazyLoading());
  }

  @PostMapping("/optimistic-lock/provoke")
  public Map<String, Object> optimisticLock() {
    scenarios.provokeOptimisticLock();
    return Map.of("updated", true);
  }

  @PostMapping("/cleanup")
  public Map<String, Object> cleanup() {
    return scenarios.cleanup();
  }
}