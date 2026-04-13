-- Extend public.notifications for scenario-driven LMS notifications.
-- Run in Supabase SQL editor if the table already exists without these columns.

alter table public.notifications
    add column if not exists scenario text,
    add column if not exists metadata jsonb not null default '{}'::jsonb,
    add column if not exists dedupe_key text;

create unique index if not exists idx_notifications_dedupe_key_unique
    on public.notifications (dedupe_key)
    where dedupe_key is not null;

create index if not exists idx_notifications_scenario
    on public.notifications (scenario)
    where scenario is not null;
