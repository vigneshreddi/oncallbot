package dev.incidentlab.inventory;

import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

/**
 * Seeds data that exposes existing bugs. Does not throw synthetic exceptions.
 * import-legacy writes reserved_qty = NULL; the NPE happens later in order-service unboxing.
 */
@RestController
@RequestMapping("/inventory/admin")
public class InventoryAdminController {
  private final JdbcTemplate jdbc;

  public InventoryAdminController(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  @PostMapping("/import-legacy")
  public Map<String, Object> importLegacy(@RequestParam(defaultValue = "SKU-LEGACY") String sku) {
    if (!"SKU-LEGACY".equals(sku) && !sku.startsWith("SKU-CHAOS-")) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Only legacy or chaos fixture SKUs are allowed");
    }
    int n = jdbc.update("""
        insert into stock(sku, available_qty, reserved_qty) values (?, 10, null)
        on conflict (sku) do update set reserved_qty = null, updated_at = now()""", sku);
    return Map.of("imported", n, "sku", sku, "reservedQty", "null");
  }

  @PostMapping("/skus/{sku}/reset")
  public Map<String, Object> resetFixture(@PathVariable String sku) {
    if (!sku.startsWith("SKU-CHAOS-")) {
      throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Only chaos fixture SKUs can be reset");
    }
    int updated = jdbc.update("""
        insert into stock(sku, available_qty, reserved_qty) values (?, 100, 0)
        on conflict (sku) do update set available_qty=100, reserved_qty=0, updated_at=now()""", sku);
    return Map.of("updated", updated, "sku", sku);
  }

  @PostMapping("/skus/{sku}/delete")
  public Map<String, Object> deleteSku(@PathVariable String sku) {
    int n = jdbc.update("delete from stock where sku=?", sku);
    return Map.of("deleted", n, "sku", sku);
  }
}
