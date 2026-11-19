-- Postgres / Supabase tables matching SQLiteStorage, for the future swap.
-- Implement a SupabaseStorage(Storage) class with the same methods as
-- verifier/storage/base.py and return it from open_storage().

create table if not exists jobs (
  id             bigserial primary key,
  name           text not null,
  source_file    text,
  source_sha256  text,
  email_column   text,
  columns_json   jsonb,
  status         text not null,
  stage_label    text,
  message        text,
  error          text,
  total_rows     integer default 0,
  created_at     double precision,
  started_at     double precision,
  finished_at    double precision,
  updated_at     double precision
);

create table if not exists emails (
  id              bigserial primary key,
  job_id          bigint not null references jobs(id) on delete cascade,
  row_index       integer,
  original_email  text,
  email           text,
  domain          text,
  status          text not null,
  stage           text not null,
  reason          text,
  mx_host         text,
  is_role         boolean default false,
  role_prefix     text,
  is_disposable   boolean default false,
  typo_fixed      text,
  smtp_code       integer,
  smtp_message    text,
  attempts        integer default 0,
  next_attempt_at double precision,
  extra_json      jsonb,
  updated_at      double precision
);
create index if not exists ix_emails_job_status       on emails(job_id, status);
create index if not exists ix_emails_job_stage_domain on emails(job_id, stage, domain);
create index if not exists ix_emails_job_updated      on emails(job_id, updated_at);

create table if not exists domains (
  domain               text primary key,
  mx_status            text,
  mx_hosts_json        jsonb,
  mx_detail            text,
  mx_checked_at        double precision,
  catch_all            smallint,
  catch_all_checked_at double precision
);
