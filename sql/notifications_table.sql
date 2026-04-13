-- Notifications aligned with BE/app/models/notification.py and UI Notification type.
-- Apply in Supabase SQL editor (adjust if public.users / public.courses differ).

create table if not exists public.notifications (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    recipient_id uuid not null references public.users (user_id) on delete cascade,
    title text not null,
    body text not null,
    notification_type text not null,
    is_read boolean not null default false,
    read_at timestamptz,
    is_pinned boolean not null default false,
    course_id bigint references public.courses (id) on delete set null,
    scenario text,
    metadata jsonb not null default '{}'::jsonb,
    dedupe_key text,
    constraint notifications_type_check check (
        notification_type in ('course', 'system', 'reminder')
    )
);

create unique index if not exists idx_notifications_dedupe_key_unique
    on public.notifications (dedupe_key)
    where dedupe_key is not null;

create index if not exists idx_notifications_recipient_created
    on public.notifications (recipient_id, created_at desc);

create index if not exists idx_notifications_course_created
    on public.notifications (course_id, created_at desc)
    where course_id is not null;

alter table public.notifications enable row level security;

-- Backend uses service role; optional policies for direct client access can be added later.
