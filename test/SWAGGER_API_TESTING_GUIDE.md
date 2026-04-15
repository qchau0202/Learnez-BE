# Swagger API Testing Guide

This guide maps tested API flows to Swagger (`/docs`) and gives ready request body examples.

Base path: `/api`

## 1) IAM

### `POST /api/iam/login`

```json
{
  "email": "learnez@email.com",
  "password": "123456"
}
```

### `POST /api/iam/accounts/` (Admin)

```json
{
  "email": "student1@email.com",
  "password": "123456",
  "role_id": 3
}
```

## 2) Courses + Modules + Enrollment

### `POST /api/courses/`

```json
{
  "title": "Software Engineering Fundamentals",
  "description": "Core software engineering concepts and practices.",
  "course_code": "SE-101",
  "semester": "1",
  "academic_year": "2025-2026",
  "lecturer_id": "b9dd5f4f-40a8-4d5a-8f5d-c63c7a9f440d",
  "is_complete": false
}
```

### `POST /api/courses/{course_id}/modules`

```json
{
  "title": "Module 1 - Requirements Engineering",
  "description": "Elicitation, analysis, and requirement specs."
}
```

### `POST /api/enrollment/{course_id}/students/{student_id}`

No JSON body (path params only).

## 3) Materials

### `POST /api/content/modules/{module_id}/materials`

`multipart/form-data`:

- `file`: binary file
- `material_type`: `file` (or your custom type)
- `max_size_mb`: `10`

### `PUT /api/content/materials/{material_id}`

`multipart/form-data`:

- optional `file` (replace file),
- optional `material_type`,
- optional `max_size_mb`.

## 4) Assignments + Submissions + Grading

### `POST /api/assignments/`

```json
{
  "module_id": 101,
  "title": "Quiz 1 - Basics",
  "description": "MCQ + essay introduction quiz.",
  "due_date": "2026-06-30T23:59:00+00:00",
  "hard_due_date": "2026-07-02T23:59:00+00:00",
  "total_score": 10.0,
  "is_graded": true,
  "questions": [
    {
      "type": "mcq",
      "content": "Choose the correct option.",
      "order_index": 0,
      "metadata": {
        "options": [
          { "id": "A", "text": "Wrong" },
          { "id": "B", "text": "Right" }
        ],
        "correct_option_ids": ["B"],
        "allow_multiple": false
      }
    }
  ]
}
```

### `POST /api/assignments/{assignment_id}/submissions`

```json
{
  "answers": [
    { "question_id": 7001, "answer_content": "{\"selected\": [\"B\"]}" },
    { "question_id": 7002, "answer_content": "Essay response content." }
  ],
  "status": "submitted"
}
```

### `PUT /api/assignments/{assignment_id}/submissions/{submission_id}`

```json
{
  "answers": [
    { "question_id": 7002, "answer_content": "Updated essay answer." }
  ],
  "status": "submitted"
}
```

### `POST /api/grading/{submission_id}/grade`

```json
{
  "answer_grades": [
    {
      "question_id": 7002,
      "earned_score": 4.5,
      "is_correct": true,
      "ai_feedback": "Good structure and clear explanation."
    }
  ],
  "feedback": "Overall good performance.",
  "finalize": true
}
```

## 5) Notifications

### `POST /api/notifications/`

```json
{
  "recipient_id": "97b31ee5-75c2-4cf1-a8c2-6ae3f2bff2c2",
  "title": "Assignment Reminder",
  "body": "Your assignment is due soon. Check /courses/42/assignments.",
  "notification_type": "reminder",
  "course_id": 42,
  "scenario": "course_announcement",
  "metadata": { "source": "manual" }
}
```

### `POST /api/notifications/bulk/mark-read`

```json
{
  "ids": [1, 2, 3]
}
```

### `PATCH /api/notifications/{notification_id}/recipient`

```json
{
  "is_read": true,
  "is_pinned": false
}
```

### `POST /api/notifications/jobs/demo-low-attendance` (Admin)

```json
{
  "student_id": "97b31ee5-75c2-4cf1-a8c2-6ae3f2bff2c2",
  "course_id": 42,
  "note": "Attendance dropped this week. Please contact your lecturer."
}
```

## Notes

- Swagger route: `/docs`
- OpenAPI JSON: `/openapi.json`
- Most request examples now come directly from model schema examples.
