package dev.incidentlab;

import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/admin/faults")
public class FaultController {
  private final JdbcTemplate jdbc;
  private final FaultRegistry registry;

  public FaultController(JdbcTemplate jdbc, FaultRegistry registry) { this.jdbc = jdbc; this.registry = registry; }

  @GetMapping
  public Map<String, Object> list() {
    return Map.of("service", service(), "faults", registry.all().keySet().stream().sorted().map(id -> Map.of("id", id, "enabled", registry.isEnabled(id))).toList());
  }

  @PostMapping("/{id}/enable")
  public Map<String, Object> enable(@PathVariable String id) {
    registry.enable(id);
    return Map.of("fault", id, "enabled", true, "at", Instant.now().toString());
  }

  @PostMapping("/{id}/disable")
  public Map<String, Object> disable(@PathVariable String id) {
    registry.disable(id);
    return Map.of("fault", id, "enabled", false);
  }

  @PostMapping("/reset")
  public Map<String, Object> reset() { registry.reset(); return Map.of("reset", true); }

  @PostMapping("/trigger")
  public Map<String, Object> trigger(@RequestParam(defaultValue = "") String fault) {
    if (fault.isBlank()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "fault is required");
    return enable(fault);
  }

  @GetMapping("/state")
  public Map<String, Object> state(@RequestParam(defaultValue = "") String fault) {
    if (isEnabled(fault)) throwFault(fault);
    return Map.of("service", service(), "healthy", true, "time", Instant.now().toString());
  }

  private boolean isEnabled(String id) { return registry.isEnabled(id); }
  private String service() { return System.getenv().getOrDefault("SERVICE_NAME", "demo-service"); }
  private void throwFault(String id) {
    switch (id) {
      case "jpa-lazy", "null-legacy" -> throw new NullPointerException("reserved quantity is null");
      case "schema-drift" -> jdbc.queryForObject("select missing_column from incident_faults", Object.class);
      case "unique-violation" -> jdbc.update("insert into incident_faults(fault_key) values (?)", "duplicate");
      case "malformed-json" -> throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "upstream returned unparsable JSON");
      case "health-down" -> throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "dependency is unavailable");
      case "oom-leak" -> { throw new OutOfMemoryError("simulated bounded fault"); }
      default -> throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "simulated fault: " + id);
    }
  }
}
