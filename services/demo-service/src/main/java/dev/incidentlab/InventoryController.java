package dev.incidentlab;

import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/inventory")
public class InventoryController {
  private final JdbcTemplate jdbc;
  private final FaultRegistry faults;

  public InventoryController(JdbcTemplate jdbc, FaultRegistry faults) { this.jdbc = jdbc; this.faults = faults; }

  public record Reserve(String sku, int qty) {}

  @PostMapping("/reserve")
  public Map<String, Object> reserve(@RequestBody Reserve req) {
    if (faults.isEnabled("health-down")) throw new IllegalStateException("inventory marked unavailable");
    if (faults.isEnabled("schema-drift")) {
      jdbc.queryForObject("select warehouse_zone from stock where sku=?", String.class, req.sku());
    }
    // Legacy rows carry NULL reserved_qty; this path preserves it and the caller unboxes it (scenario A).
    jdbc.update("update stock set reserved_qty = reserved_qty + ?, updated_at=now() where sku=?", req.qty(), req.sku());
    Map<String, Object> row = jdbc.queryForMap("select sku, available_qty, reserved_qty from stock where sku=?", req.sku());
    return Map.of("sku", row.get("sku"), "availableQty", row.get("available_qty"), "reservedQty", row.get("reserved_qty"));
  }

  // Legacy import leaves reserved_qty NULL; downstream unboxing fails (scenario A).
  @PostMapping("/admin/import-legacy")
  public Map<String, Object> importLegacy() {
    int n = jdbc.update("insert into stock(sku, available_qty, reserved_qty) values ('SKU-LEGACY', 10, null) on conflict (sku) do update set reserved_qty = null, updated_at = now()");
    return Map.of("imported", n, "sku", "SKU-LEGACY");
  }
}
