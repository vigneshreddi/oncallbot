package dev.incidentlab;

import java.time.Instant;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class BusinessController {
  private final JdbcTemplate jdbc;
  private final FaultRegistry registry;
  public BusinessController(JdbcTemplate jdbc, FaultRegistry registry) { this.jdbc = jdbc; this.registry = registry; }

  @GetMapping("/health")
  public Map<String, Object> health() { return Map.of("service", service(), "status", "UP", "time", Instant.now().toString()); }

  @PostMapping("/work")
  public Map<String, Object> work(@RequestParam(defaultValue = "ok") String mode) {
    jdbc.update("create table if not exists incident_faults (id bigserial primary key, fault_key text, created_at timestamptz default now(), updated_at timestamptz default now())");
    if (registry.isEnabled("jpa-lazy") || registry.isEnabled("null-legacy")) throw new NullPointerException("reserved quantity is null");
    if (registry.isEnabled("schema-drift")) jdbc.queryForObject("select missing_column from incident_faults", Object.class);
    if (registry.isEnabled("health-down")) throw new IllegalStateException("dependency is unavailable");
    if (registry.isEnabled("malformed-json")) throw new IllegalStateException("upstream returned unparsable JSON");
    if ("fail".equals(mode)) throw new IllegalStateException("business operation failed");
    return Map.of("service", service(), "accepted", true, "time", Instant.now().toString());
  }

  private String service() { return System.getenv().getOrDefault("SERVICE_NAME", "demo-service"); }
}
