## Table `assignment_questions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `assignment_id` | `int8` |  Nullable |
| `created_at` | `timestamptz` |  |
| `type` | `text` |  Nullable |
| `content` | `text` |  Nullable |
| `order_index` | `int8` |  Nullable |
| `metadata` | `jsonb` |  Nullable |

## Table `assignment_submission_answers`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `submission_id` | `int8` |  Nullable |
| `question_id` | `int8` |  Nullable |
| `answer_content` | `text` |  Nullable |
| `is_correct` | `bool` |  Nullable |
| `earned_score` | `numeric` |  Nullable |
| `ai_feedback` | `text` |  Nullable |

## Table `assignment_submissions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `student_id` | `uuid` |  Nullable |
| `is_corrected` | `bool` |  Nullable |
| `assignment_id` | `int8` |  Nullable |
| `status` | `text` |  Nullable |
| `final_score` | `numeric` |  Nullable |
| `feedback` | `text` |  Nullable |
| `risk_score` | `numeric` |  Nullable |
| `submitted_at` | `timestamptz` |  Nullable |
| `elapsed_time` | `int8` |  Nullable |
| `is_late` | `bool` |  Nullable |

## Table `assignments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `module_id` | `int8` |  Nullable |
| `created_at` | `timestamptz` |  |
| `description` | `varchar` |  Nullable |
| `due_date` | `timestamptz` |  Nullable |
| `total_score` | `numeric` |  Nullable |
| `title` | `text` |  Nullable |
| `is_graded` | `bool` |  Nullable |
| `uploaded_by` | `uuid` |  Nullable |
| `hard_due_date` | `timestamptz` |  Nullable |
| `duration` | `int8` |  Nullable |
| `duration_enabled` | `bool` |  Nullable |

## Table `course_attendance`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `recorded_by` | `uuid` |  Nullable |
| `student_id` | `uuid` |  Nullable |
| `course_id` | `int8` |  Nullable |
| `status` | `text` |  Nullable |
| `session_date` | `timestamptz` |  Nullable |
| `notes` | `text` |  Nullable |

## Table `course_enrollments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `created_at` | `timestamptz` |  |
| `course_id` | `int8` | Primary |
| `student_id` | `uuid` | Primary |

## Table `courses`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `title` | `varchar` |  Nullable |
| `description` | `varchar` |  Nullable |
| `lecturer_id` | `uuid` |  Nullable |
| `is_complete` | `bool` |  Nullable |
| `course_code` | `text` |  Nullable |
| `created_by` | `uuid` |  Nullable |
| `semester` | `int8` |  Nullable |
| `academic_year` | `text` |  Nullable |
| `class_room` | `text` |  Nullable |
| `course_occurences` | `int8` |  Nullable |
| `course_session` | `text` |  Nullable |
| `course_start_date` | `date` |  Nullable |
| `course_end_date` | `date` |  Nullable |
| `from_department` | `int8` |  Nullable |
| `course_session_date` | `text` |  Nullable |
| `course_session_duration` | `int8` |  Nullable |

## Table `departments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `name` | `text` |  Nullable |
| `department_code` | `text` |  Nullable |
| `from_faculty` | `int8` |  Nullable |

## Table `faculties`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `name` | `text` |  Nullable |
| `faculty_code` | `text` |  Nullable |

## Table `lecturer_profiles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `created_at` | `timestamp` |  Nullable |
| `phone_number` | `text` |  Nullable |
| `lecturer_id` | `text` |  Nullable |
| `gender` | `text` |  Nullable |
| `qualification` | `text` |  Nullable |
| `faculty_id` | `int8` |  Nullable |
| `department_id` | `int8` |  Nullable |

## Table `module_materials`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `module_id` | `int8` |  Nullable |
| `created_at` | `timestamptz` |  |
| `material_type` | `varchar` |  Nullable |
| `file_url` | `text` |  Nullable |
| `uploaded_by` | `uuid` |  Nullable |
| `storage_provider` | `text` |  |
| `cloudinary_public_id` | `text` |  Nullable |
| `mime_type` | `text` |  Nullable |
| `size_bytes` | `int8` |  Nullable |
| `metadata` | `jsonb` |  |
| `name` | `text` |  Nullable |
| `description` | `text` |  Nullable |

## Table `modules`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `title` | `varchar` |  Nullable |
| `description` | `varchar` |  Nullable |
| `course_id` | `int8` |  Nullable |

## Table `notifications`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `recipient_id` | `uuid` |  |
| `title` | `text` |  |
| `body` | `text` |  |
| `notification_type` | `text` |  |
| `is_read` | `bool` |  |
| `read_at` | `timestamptz` |  Nullable |
| `is_pinned` | `bool` |  |
| `course_id` | `int8` |  Nullable |
| `scenario` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `dedupe_key` | `text` |  Nullable |

## Table `permissions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `permission_id` | `int8` | Primary Identity |
| `permission_name` | `text` |  |
| `description` | `text` |  Nullable |

## Table `role_permissions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `created_at` | `timestamptz` |  |
| `role_id` | `int8` |  Nullable |
| `permission_id` | `int8` |  Nullable |

## Table `roles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `role_id` | `int8` | Primary Identity |
| `role_name` | `text` |  |

## Table `student_files`

Student file metadata linked to Cloudinary or Supabase bucket storage with versioning support

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `student_id` | `uuid` |  |
| `folder_id` | `int8` |  Nullable |
| `file_name` | `text` |  |
| `file_title` | `text` |  |
| `description` | `text` |  Nullable |
| `file_url` | `text` |  Nullable |
| `mime_type` | `text` |  Nullable |
| `size_bytes` | `int8` |  Nullable |
| `storage_provider` | `text` |  |
| `cloudinary_public_id` | `text` |  Nullable |
| `supabase_storage_path` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `uploaded_by` | `uuid` |  Nullable |
| `is_deleted` | `bool` |  |
| `metadata` | `jsonb` |  |

## Table `student_folders`

Hierarchical folder structure for student file organization with soft delete support

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary Identity |
| `student_id` | `uuid` |  |
| `parent_folder_id` | `int8` |  Nullable |
| `folder_name` | `text` |  |
| `description` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `is_deleted` | `bool` |  |
| `metadata` | `jsonb` |  |

## Table `student_profiles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `created_at` | `timestamp` |  Nullable |
| `phone_number` | `text` |  Nullable |
| `student_id` | `text` |  Nullable |
| `gender` | `text` |  Nullable |
| `faculty_id` | `int8` |  Nullable |
| `major` | `text` |  Nullable |
| `enrolled_year` | `int8` |  Nullable |
| `date_of_birth` | `date` |  Nullable |
| `current_gpa` | `numeric` |  Nullable |
| `cumulative_gpa` | `numeric` |  Nullable |
| `department_id` | `int8` |  Nullable |
| `class` | `text` |  Nullable |

## Table `users`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `full_name` | `text` |  Nullable |
| `email` | `text` |  Nullable |
| `role_id` | `int8` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamp` |  Nullable |
| `created_by` | `uuid` |  Nullable |

