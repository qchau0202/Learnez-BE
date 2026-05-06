-- Strict RBAC reset script
-- 1) Drops per-user permission override table.
-- 2) Rebuilds role_permissions for Admin / Lecturer / Student.
--
-- Run this in Supabase SQL editor (or psql) as a privileged role.

begin;

-- Drop the only table that enables per-user permission overrides.
drop table if exists public.user_permissions cascade;

-- Ensure role_permissions exists.
create table if not exists public.role_permissions (
  role_id integer not null,
  permission_id integer not null,
  created_at timestamptz default now(),
  primary key (role_id, permission_id)
);

-- Clear existing assignments.
delete from public.role_permissions;

-- Reinsert strict defaults by role name.
with role_map as (
  select role_id, lower(trim(role_name)) as role_name
  from public.roles
),
strict_pairs as (
  -- Admin = all permissions
  select r.role_id, p.permission_id
  from role_map r
  join public.permissions p on true
  where r.role_name like '%admin%'

  union all

  -- Lecturer strict defaults:
  -- course-02, course-03, module-01..04, material-01..04, assignment-01..04
  select r.role_id, p.permission_id
  from role_map r
  join public.permissions p
    on p.permission_name in (
      'course-02','course-03',
      'module-01','module-02','module-03','module-04',
      'material-01','material-02','material-03','material-04',
      'assignment-01','assignment-02','assignment-03','assignment-04'
    )
  where r.role_name = 'lecturer'

  union all

  -- Student strict defaults:
  -- read-only learning permissions
  select r.role_id, p.permission_id
  from role_map r
  join public.permissions p
    on p.permission_name in (
      'course-02',
      'module-02',
      'material-02',
      'assignment-02'
    )
  where r.role_name = 'student'
)
insert into public.role_permissions (role_id, permission_id)
select distinct role_id, permission_id
from strict_pairs;

commit;
