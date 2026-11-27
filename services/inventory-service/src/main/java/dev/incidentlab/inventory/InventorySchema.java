package dev.incidentlab.inventory;

import jakarta.annotation.PostConstruct;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

@Component
public class InventorySchema {
  private final JdbcTemplate jdbc;

  public InventorySchema(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  @PostConstruct
  public void init() {
    jdbc.execute("set search_path to inventory");
    jdbc.execute("""
        create table if not exists stock (
          sku text primary key,
          available_qty integer not null,
          reserved_qty integer,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now())""");
    jdbc.update("insert into stock(sku, available_qty, reserved_qty) values ('SKU-1', 100, 0) on conflict do nothing");
    jdbc.update("insert into stock(sku, available_qty, reserved_qty) values ('SKU-2', 50, 0) on conflict do nothing");
  }
}
