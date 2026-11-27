package dev.incidentlab.inventory;

import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/inventory")
public class StockPlacementController {
  private final JdbcTemplate jdbc;

  public StockPlacementController(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  /**
   * BUG: application code expects stock.warehouse_zone, but the schema was never migrated (SQLSTATE 42703).
   * This is a real query, not a simulated exception.
   */
  @GetMapping("/placement/{sku}")
  public Map<String, Object> placement(@PathVariable String sku) {
    return jdbc.queryForMap("select sku, warehouse_zone from stock where sku=?", sku);
  }
}
