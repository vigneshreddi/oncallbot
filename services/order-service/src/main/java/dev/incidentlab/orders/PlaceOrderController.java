package dev.incidentlab.orders;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/orders")
public class PlaceOrderController {
  private final JdbcTemplate jdbc;
  private final RestClient inventory;
  private final RestClient payment;

  public PlaceOrderController(JdbcTemplate jdbc, RestClient.Builder builder,
      @Value("${app.peers.inventory}") String inventoryUrl, @Value("${app.peers.payment}") String paymentUrl) {
    this.jdbc = jdbc;
    this.inventory = builder.clone().baseUrl(inventoryUrl).build();
    this.payment = builder.clone().baseUrl(paymentUrl).build();
  }

  public record PlaceOrder(String customerId, String sku, int qty) {}

  public record Reservation(String sku, Integer reservedQty, Integer availableQty) {
    public int reservedQtyOrZero() {
      return reservedQty == null ? 0 : reservedQty;
    }

    public int availableQtyOrZero() {
      return availableQty == null ? 0 : availableQty;
    }
  }

  /** Incomplete contract: payment-service can return AUTHORIZED, which Jackson cannot map. */
  public enum PaymentStatus { CAPTURED, FAILED }

  public record PaymentResult(long orderId, PaymentStatus status) {}

  @PostMapping
  public Map<String, Object> place(@RequestBody PlaceOrder req) {
    Long orderId = jdbc.queryForObject(
        "insert into orders(customer_id, sku, qty, status) values (?,?,?,'PENDING') returning id",
        Long.class, req.customerId(), req.sku(), req.qty());

    Reservation res = inventory.post().uri("/inventory/reserve")
        .body(Map.of("sku", req.sku(), "qty", req.qty()))
        .retrieve().body(Reservation.class);

    if (res == null) {
      throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "inventory returned no reservation");
    }
    if (res.reservedQty() == null || res.availableQty() == null) {
      throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "inventory returned incomplete reservation: " + res);
    }
    int reserved = res.reservedQtyOrZero();
    int remaining = res.availableQtyOrZero() - reserved;

    PaymentResult pay = chargeWithRetry(orderId, req.customerId());
    if (pay.status() != PaymentStatus.CAPTURED) {
      throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "payment not captured: " + pay.status());
    }
    jdbc.update("update orders set status='CONFIRMED', updated_at=now() where id=?", orderId);
    return Map.of("orderId", orderId, "status", "CONFIRMED", "remainingStock", remaining, "at", Instant.now().toString());
  }

  /**
   * BUG: always retries the charge once on transport/timeout failure, with no idempotency key.
   * payment-service inserts on every call, so a late first insert plus retry hits unique(order_id).
   */
  private PaymentResult chargeWithRetry(long orderId, String customerId) {
    Map<String, Object> body = Map.of("orderId", orderId, "customerId", customerId, "amount", new BigDecimal("19.99"));
    try {
      return payment.post().uri("/payments/charge").body(body).retrieve().body(PaymentResult.class);
    } catch (RestClientException first) {
      return payment.post().uri("/payments/charge").body(body).retrieve().body(PaymentResult.class);
    }
  }

  @GetMapping("/{id}")
  public Map<String, Object> get(@PathVariable long id) {
    return jdbc.queryForMap("select id, customer_id, sku, qty, status, created_at, updated_at from orders where id=?", id);
  }
}
