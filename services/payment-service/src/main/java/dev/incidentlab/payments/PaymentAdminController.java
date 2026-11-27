package dev.incidentlab.payments;

import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

/**
 * Seeds data that exposes existing bugs. Does not throw synthetic exceptions.
 * slow tier makes charge exceed order-service's 1.5s read timeout; AUTHORIZED is not in the order enum.
 */
@RestController
@RequestMapping("/payments/admin")
public class PaymentAdminController {
  private final JdbcTemplate jdbc;

  public PaymentAdminController(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  @PostMapping("/customers/{id}/tier")
  public Map<String, Object> setTier(@PathVariable String id, @RequestParam String tier) {
    jdbc.update("""
        insert into customer_tiers(customer_id, tier) values (?,?)
        on conflict (customer_id) do update set tier=excluded.tier, updated_at=now()""", id, tier);
    return Map.of("customerId", id, "tier", tier);
  }

  @PostMapping("/capture-mode")
  public Map<String, Object> setCaptureMode(@RequestParam String mode) {
    jdbc.update("update billing_policy set capture_mode=?, updated_at=now() where id=1", mode);
    return Map.of("captureMode", mode);
  }

  @PostMapping("/customers/{id}/tier/delete")
  public Map<String, Object> deleteFixtureTier(@PathVariable String id) {
    if (!id.startsWith("chaos-")) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Only chaos fixture tiers can be deleted");
    }
    int deleted = jdbc.update("delete from customer_tiers where customer_id=?", id);
    return Map.of("deleted", deleted, "customerId", id);
  }

  @PostMapping("/charges/{orderId}/delete")
  public Map<String, Object> deleteFixtureCharge(@PathVariable long orderId) {
    if (orderId >= 0) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Only negative fixture order IDs can be deleted");
    }
    int deleted = jdbc.update("delete from payments where order_id=?", orderId);
    return Map.of("deleted", deleted, "orderId", orderId);
  }

  @GetMapping("/policy")
  public Map<String, Object> policy() {
    return jdbc.queryForMap("select capture_mode, updated_at from billing_policy where id=1");
  }
}
