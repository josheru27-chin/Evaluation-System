import csv
from io import TextIOWrapper
from collections import defaultdict

from django.contrib import messages
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.contrib import messages
from django.shortcuts import redirect
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

from ..models import (
    DepartmentHead,
    FacultyMember,
    EvaluationSchedule,
    FacultyEvaluation,
)

LOGIN_LINK_MAX_AGE = 300
LINK_SALT = "faculty-eval-login"

def _get_logged_in_head(request):
    is_head_authenticated = request.session.get("is_head_authenticated")
    head_id = request.session.get("head_id")
    department_id = request.session.get("department_id")

    if not is_head_authenticated or not head_id or not department_id:
        return None

    return (
        DepartmentHead.objects
        .select_related("department")
        .filter(id=head_id, department_id=department_id)
        .first()
    )


def _get_open_schedule():
    now = timezone.localtime(timezone.now())
    return (
        EvaluationSchedule.objects
        .filter(start_datetime__lte=now, end_datetime__gte=now)
        .order_by("start_datetime")
        .first()
    )

def head_add(request):
    logged_in_head = _get_logged_in_head(request)
    if not logged_in_head:
        messages.error(request, "Please log in first.")
        return redirect("admin_login")

    faculty_qs = (
        FacultyMember.objects
        .filter(department=logged_in_head.department)
        .order_by("name")
    )

    if request.method == "POST":
        uploaded_file = request.FILES.get("faculty_file")
        print("FILES:", request.FILES)
        print("UPLOADED FILE:", uploaded_file)

        if not uploaded_file:
            messages.error(request, "Please upload a CSV file.")
            return redirect("head_add")

        if not uploaded_file.name.lower().endswith(".csv"):
            messages.error(request, "Only CSV files are allowed.")
            return redirect("head_add")

        created_count = 0

        try:
            decoded_file = TextIOWrapper(uploaded_file.file, encoding="utf-8")
            reader = csv.DictReader(decoded_file)

            faculty_to_create = []

            for row in reader:
                normalized = {
                    str(k).strip().upper(): (str(v).strip() if v else "")
                    for k, v in row.items()
                }

                name = normalized.get("NAME", "")
                id_number = normalized.get("ID NUMBER", "")
                email = normalized.get("GSFE EMAIL", "") or normalized.get("EMAIL", "")

                if not name:
                    first_name = normalized.get("FIRST NAME", "")
                    last_name = normalized.get("LAST NAME", "")
                    name = f"{first_name} {last_name}".strip()

                if not name:
                    continue

                faculty_to_create.append(
                    FacultyMember(
                        department=logged_in_head.department,
                        id_number=id_number,
                        name=name,
                        email=email,
                    )
                )

            if faculty_to_create:
                with transaction.atomic():
                    FacultyMember.objects.bulk_create(faculty_to_create)
                created_count = len(faculty_to_create)

            messages.success(request, f"{created_count} faculty record(s) added successfully.")
            return redirect("head_add")

        except Exception as e:
            messages.error(request, f"Upload failed: {str(e)}")
            return redirect("head_add")

    context = {
        "logged_in_head": logged_in_head,
        "faculty_members": faculty_qs,
        "faculty_count": faculty_qs.count(),
    }

    return render(request, "head/head_add.html", context)

def head_monitor(request):
    logged_in_head = _get_logged_in_head(request)
    if not logged_in_head:
        messages.error(request, "Please log in first.")
        return redirect("eval_login")

    open_schedule = _get_open_schedule()

    faculty_members = list(
        FacultyMember.objects
        .filter(department=logged_in_head.department)
        .order_by("name")
    )

    faculty_ids = [f.id for f in faculty_members]

    evaluations = (
        FacultyEvaluation.objects
        .filter(evaluatee_faculty_id__in=faculty_ids)
        .select_related("evaluatee_faculty", "evaluator_head", "schedule")
        .prefetch_related("responses")
        .order_by("-submitted_at")
    )

    if open_schedule:
        evaluations = evaluations.filter(schedule=open_schedule)

    grouped = defaultdict(lambda: {
        "faculty": None,
        "evaluators": [],
        "overall_values": [],
        "total_scores": [],
    })

    for evaluation in evaluations:
        faculty = evaluation.evaluatee_faculty
        if not faculty:
            continue

        grouped[faculty.id]["faculty"] = faculty
        grouped[faculty.id]["evaluators"].append({
            "name": evaluation.evaluator_name,
            "department": evaluation.evaluator_department,
            "average_score": float(evaluation.average_score or 0),
            "total_score": float(evaluation.total_score or 0),
            "submitted_at": evaluation.submitted_at,
        })
        grouped[faculty.id]["overall_values"].append(float(evaluation.average_score or 0))
        grouped[faculty.id]["total_scores"].append(float(evaluation.total_score or 0))

    results = []
    completed_count = 0

    for index, faculty in enumerate(faculty_members, start=1):
        item = grouped.get(faculty.id)
        evaluators = item["evaluators"] if item else []
        evaluator_count = len(evaluators)
        average_score = round(sum(item["overall_values"]) / evaluator_count, 2) if evaluator_count else 0
        average_total_score = round(sum(item["total_scores"]) / evaluator_count, 2) if evaluator_count else 0

        # 15 questions x max 5 = 75
        computed_rating = round((average_total_score / 75) * 100, 2) if average_total_score else 0

        if evaluator_count > 0:
            completed_count += 1

        results.append({
            "num": index,
            "id": faculty.id,
            "name": faculty.name,
            "id_number": faculty.id_number,
            "email": faculty.email,
            "department": faculty.department.name,
            "position": "Faculty Member",
            "evaluator_count": evaluator_count,
            "average_score": average_score,
            "average_total_score": average_total_score,
            "computed_rating": computed_rating,
            "evaluators": evaluators,
        })

    total_instructors = len(faculty_members)
    pending_reviews = max(total_instructors - completed_count, 0)
    dept_avg_score = round(
        sum(r["computed_rating"] for r in results) / len([r for r in results if r["evaluator_count"] > 0]),
        2
    ) if any(r["evaluator_count"] > 0 for r in results) else 0

    context = {
        "logged_in_head": logged_in_head,
        "open_schedule": open_schedule,
        "results": results,
        "total_instructors": total_instructors,
        "evaluations_complete": completed_count,
        "pending_reviews": pending_reviews,
        "dept_avg_score": dept_avg_score,
    }
    return render(request, "head/head_monitor.html", context)



def verify_head_login_link(request, token):
    signer = TimestampSigner(salt=LINK_SALT)

    try:
        unsigned_value = signer.unsign(token, max_age=LOGIN_LINK_MAX_AGE)
        head_id = int(unsigned_value)
    except SignatureExpired:
        messages.error(request, "This login link has expired. Please request a new one.")
        return redirect("admin_login")
    except (BadSignature, ValueError):
        messages.error(request, "This login link is invalid.")
        return redirect("admin_login")

    logged_in_head = (
        DepartmentHead.objects
        .select_related("department")
        .filter(id=head_id)
        .first()
    )

    if not logged_in_head:
        messages.error(request, "Head account not found.")
        return redirect("admin_login")

    request.session["head_id"] = logged_in_head.id
    request.session["head_name"] = logged_in_head.name
    request.session["head_email"] = logged_in_head.email
    request.session["department_id"] = logged_in_head.department.id
    request.session["department_name"] = logged_in_head.department.name
    request.session["is_head_authenticated"] = True

    messages.success(request, f"Welcome, {logged_in_head.name}.")
    return redirect("head_monitor")