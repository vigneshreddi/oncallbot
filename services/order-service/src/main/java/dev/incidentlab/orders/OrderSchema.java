package dev.incidentlab.orders;

import jakarta.annotation.PostConstruct;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

@Component
public class OrderSchema {
  private final JdbcTemplate jdbc;

  public OrderSchema(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  @PostConstruct
  public void init() {
    jdbc.execute("set search_path to orders");
    jdbc.execute("""
        create table if not exists orders (
          id bigserial primary key,
          customer_id text not null,
          sku text not null,
          qty integer not null,
          status text not null,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now())""");
  }
}
