package dev.incidentlab.payments;

import jakarta.annotation.PostConstruct;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

@Component
public class PaymentSchema {
  private final JdbcTemplate jdbc;

  public PaymentSchema(JdbcTemplate jdbc) {
    this.jdbc = jdbc;
  }

  @PostConstruct
  public void init() {
    jdbc.execute("set search_path to payments");
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
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now())""");
    jdbc.execute("""
        create table if not exists billing_policy (
          id integer primary key,
          capture_mode text not null,
          updated_at timestamptz not null default now())""");
    jdbc.update("insert into billing_policy(id, capture_mode) values (1, 'CAPTURED') on conflict (id) do nothing");
  }
}
