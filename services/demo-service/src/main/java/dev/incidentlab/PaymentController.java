package dev.incidentlab;

import java.math.BigDecimal;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/payments")
public class PaymentController {
  private final JdbcTemplate jdbc;
  private final FaultRegistry faults;

  public PaymentController(JdbcTemplate jdbc, FaultRegistry faults) { this.jdbc = jdbc; this.faults = faults; }

  public record Charge(long orderId, String customerId, BigDecimal amount) {}

  @PostMapping("/charge")
  public Map<String, Object> charge(@RequestBody Charge req) throws InterruptedException {
    String tier = jdbc.query("select tier from customer_tiers where customer_id=?",
        rs -> rs.next() ? rs.getString(1) : "standard", req.customerId());
    if ("slow".equals(tier)) Thread.sleep(3000);
    if (faults.isEnabled("slow-endpoint")) Thread.sleep(3000);

    // Scenario C: new upstream status the order-service enum does not know.
    String status = faults.isEnabled("status-enum") ? "AUTHORIZED" : "CAPTURED";
    jdbc.update("insert into payments(order_id, amount, status) values (?,?,?)", req.orderId(), req.amount(), status);
    return Map.of("orderId", req.orderId(), "status", status);
  }

  @PostMapping("/admin/customers/{id}/tier")
  public Map<String, Object> setTier(@PathVariable String id, @RequestParam String tier) {
    jdbc.update("insert into customer_tiers(customer_id, tier) values (?,?) on conflict (customer_id) do update set tier=excluded.tier, updated_at=now()", id, tier);
    return Map.of("customerId", id, "tier", tier);
  }
}
