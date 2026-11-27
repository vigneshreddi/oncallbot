package dev.incidentlab;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestClient;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/orders")
public class OrderController {
  private final JdbcTemplate jdbc;
  private final FaultRegistry faults;
  private final RestClient inventory;
  private final RestClient payment;

  public OrderController(JdbcTemplate jdbc, FaultRegistry faults, RestClient.Builder builder,
      @Value("${app.peers.inventory}") String inventoryUrl, @Value("${app.peers.payment}") String paymentUrl) {
    this.jdbc = jdbc;
    this.faults = faults;
    this.inventory = builder.baseUrl(inventoryUrl).build();
    this.payment = builder.baseUrl(paymentUrl).build();
  }

  public record PlaceOrder(String customerId, String sku, int qty) {}
  public record Reservation(String sku, Integer reservedQty, Integer availableQty) {}
  public record PaymentResult(long orderId, PaymentStatus status) {}
  public enum PaymentStatus { CAPTURED, FAILED }

  @PostMapping
  public Map<String, Object> place(@RequestBody PlaceOrder req) {
    Long orderId = jdbc.queryForObject(
        "insert into orders(customer_id, sku, qty, status) values (?,?,?,'PENDING') returning id",
        Long.class, req.customerId(), req.sku(), req.qty());

    Reservation res = inventory.post().uri("/inventory/reserve").body(Map.of("sku", req.sku(), "qty", req.qty()))
        .retrieve().body(Reservation.class);
    int remaining = res.availableQty() - res.reservedQty();

    PaymentResult pay;
    try {
      pay = payment.post().uri("/payments/charge")
          .body(Map.of("orderId", orderId, "customerId", req.customerId(), "amount", new BigDecimal("19.99")))
          .retrieve().body(PaymentResult.class);
    } catch (Exception first) {
      if (faults.isEnabled("retry-dup-payment")) {
        pay = payment.post().uri("/payments/charge")
            .body(Map.of("orderId", orderId, "customerId", req.customerId(), "amount", new BigDecimal("19.99")))
            .retrieve().body(PaymentResult.class);
      } else {
        throw first;
      }
    }
    if (pay.status() != PaymentStatus.CAPTURED) {
      throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "payment not captured: " + pay.status());
    }
    jdbc.update("update orders set status='CONFIRMED', updated_at=now() where id=?", orderId);
    return Map.of("orderId", orderId, "status", "CONFIRMED", "remainingStock", remaining, "at", Instant.now().toString());
  }

  @GetMapping("/{id}")
  public Map<String, Object> get(@PathVariable long id) {
    return jdbc.queryForMap("select id, customer_id, sku, qty, status, created_at, updated_at from orders where id=?", id);
  }
}
