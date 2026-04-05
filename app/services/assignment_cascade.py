"""DB cascade deletes for assignments (shared by assessment and course routes)."""


def delete_assignment_cascade(sb, assignment_id: int) -> None:
    subs = sb.table("assignment_submissions").select("id").eq("assignment_id", assignment_id).execute()
    for s in subs.data or []:
        sid = s["id"]
        sb.table("assignment_submission_answers").delete().eq("submission_id", sid).execute()
    sb.table("assignment_submissions").delete().eq("assignment_id", assignment_id).execute()
    sb.table("assignment_questions").delete().eq("assignment_id", assignment_id).execute()
    sb.table("assignments").delete().eq("id", assignment_id).execute()
