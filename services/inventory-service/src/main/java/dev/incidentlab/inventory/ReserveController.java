package dev.incidentlab.inventory;

import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/inventory")
public class ReserveController {
  private final JdbcTemplate jdbc;

  public ReserveController(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  public record Reserve(String sku, int qty) {}

  @PostMapping("/reserve")
  public Map<String, Object> reserve(@RequestBody Reserve req) {
    // BUG: NULL + qty is NULL in SQL. Legacy rows with reserved_qty NULL stay NULL after "reservation".
    jdbc.update("update stock set reserved_qty = reserved_qty + ?, updated_at=now() where sku=?", req.qty(), req.sku());
    Map<String, Object> row = jdbc.queryForMap("select sku, available_qty, reserved_qty from stock where sku=?", req.sku());
    Map<String, Object> body = new LinkedHashMap<>();
    body.put("sku", row.get("sku"));
    body.put("availableQty", row.get("available_qty"));
    body.put("reservedQty", row.get("reserved_qty"));
    return body;
  }

  @GetMapping("/stock/{sku}")
  public Map<String, Object> get(@PathVariable String sku) {
    return jdbc.queryForMap("select sku, available_qty, reserved_qty, created_at, updated_at from stock where sku=?", sku);
  }
}
