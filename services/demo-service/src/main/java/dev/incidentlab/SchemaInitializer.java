package dev.incidentlab;

import jakarta.annotation.PostConstruct;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

@Component
public class SchemaInitializer {
  private final JdbcTemplate jdbc;
  private final String schema = System.getenv().getOrDefault("DB_SCHEMA", "public");

  public SchemaInitializer(JdbcTemplate jdbc) { this.jdbc = jdbc; }

  @PostConstruct
  public void init() {
    jdbc.execute("set search_path to " + schema);
    switch (schema) {
      case "orders" -> {
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
      case "inventory" -> {
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
      case "payments" -> {
        jdbc.execute("""
            create table if not exists payments (
              id bigserial primary key,
              order_id bigint not null unique,
              amount numeric(10,2) not null,
              status text not null,
              created_at timestamptz not null default now(),
              updated_at timestamptz not null default now())""");
        jdbc.execute("""
            create table if not exists customer_tiers (
              customer_id text primary key,
              tier text not null,
              updated_at timestamptz not null default now())""");
      }
      default -> {}
    }
  }
}
