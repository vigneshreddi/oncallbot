create table if not exists incident_faults (
  id bigserial primary key,
  fault_key text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
