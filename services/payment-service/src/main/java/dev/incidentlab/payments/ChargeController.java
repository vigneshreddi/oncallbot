package dev.incidentlab.payments;

import java.math.BigDecimal;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/payments")
public class ChargeController {
  private final JdbcTemplate jdbc;

  public ChargeController(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  public record Charge(long orderId, String customerId, BigDecimal amount) {}

  @PostMapping("/charge")
  public Map<String, Object> charge(@RequestBody Charge req) throws InterruptedException {
    String tier = jdbc.query("select tier from customer_tiers where customer_id=?",
        rs -> rs.next() ? rs.getString(1) : "standard", req.customerId());
    if ("slow".equals(tier)) {
      Thread.sleep(3000);
    }
    String status = jdbc.queryForObject("select capture_mode from billing_policy where id=1", String.class);
    // BUG: every call inserts a new row. Unique(order_id) fails when order-service retries after a timeout.
    jdbc.update("insert into payments(order_id, amount, status) values (?,?,?)", req.orderId(), req.amount(), status);
    return Map.of("orderId", req.orderId(), "status", status);
  }
}
