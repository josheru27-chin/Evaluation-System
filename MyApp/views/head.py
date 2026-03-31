import csv
from io import TextIOWrapper
from collections import defaultdict

from django.contrib import messages
from django.db import transaction
from django.shortcuts import render, redirect
from django.utils import timezone
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db.models import Prefetch, Case, When, IntegerField

from ..models import (
    DepartmentHead,
    FacultyMember,
    EvaluationSchedule,
    FacultyEvaluation,
    FacultyEvaluationResponse,
)

LOGIN_LINK_MAX_AGE = 300
LINK_SALT = "faculty-eval-login"


def _get_open_schedule():
    now = timezone.localtime(timezone.now())
    return (
        EvaluationSchedule.objects
        .filter(start_datetime__lte=now, end_datetime__gte=now)
        .order_by("start_datetime")
        .first()
    )


def _get_logged_in_head(request):
    is_head_authenticated = request.session.get("is_head_authenticated")
    head_id = request.session.get("head_id")
    department_id = request.session.get("department_id")

    if not is_head_authenticated or not head_id or not department_id:
        return None

    return (
        DepartmentHead.objects
        .select_related("department", "schedule")
        .filter(id=head_id, department_id=department_id)
        .order_by("-schedule__start_datetime", "-id")
        .first()
    )


def _get_latest_schedule_for_head_dashboard(logged_in_head):
    """
    Dashboard priority:
    1. Current open schedule for the head's department, if any
    2. Latest schedule that has faculty for the head's department
    3. Latest schedule that has department-head record for that department
    """
    open_schedule = (
        EvaluationSchedule.objects
        .filter(
            start_datetime__lte=timezone.localtime(timezone.now()),
            end_datetime__gte=timezone.localtime(timezone.now()),
            faculty_members__department=logged_in_head.department,
        )
        .distinct()
        .order_by("start_datetime")
        .first()
    )
    if open_schedule:
        return open_schedule

    latest_faculty_schedule = (
        EvaluationSchedule.objects
        .filter(faculty_members__department=logged_in_head.department)
        .distinct()
        .order_by("-start_datetime", "-created_at")
        .first()
    )
    if latest_faculty_schedule:
        return latest_faculty_schedule

    latest_head_schedule = (
        EvaluationSchedule.objects
        .filter(department_heads__department=logged_in_head.department)
        .distinct()
        .order_by("-start_datetime", "-created_at")
        .first()
    )
    return latest_head_schedule


def _ordered_response_queryset(model):
    return model.objects.annotate(
        section_order=Case(
            When(section_code="management_teaching_learning", then=0),
            When(section_code="content_knowledge_pedagogy_technology", then=1),
            When(section_code="commitment_transparency", then=2),
            default=99,
            output_field=IntegerField(),
        )
    ).order_by("section_order", "question_number")


def _build_head_results_for_schedule(logged_in_head, schedule):
    faculty_members = list(
        FacultyMember.objects
        .filter(schedule=schedule, department=logged_in_head.department)
        .order_by("name")
    )

    faculty_ids = [f.id for f in faculty_members]

    evaluations = (
        FacultyEvaluation.objects
        .filter(
            schedule=schedule,
            evaluatee_faculty_id__in=faculty_ids,
            status="submitted"
        )
        .select_related("evaluatee_faculty", "evaluator_head", "schedule")
        .prefetch_related(
            Prefetch(
                "responses",
                queryset=_ordered_response_queryset(FacultyEvaluationResponse),
            )
        )
        .order_by("-submitted_at")
    )

    grouped = defaultdict(lambda: {
        "faculty": None,
        "evaluators": [],
        "overall_values": [],
        "total_scores": [],
        "computed_ratings": [],
        "section_values": defaultdict(list),
    })

    for evaluation in evaluations:
        faculty = evaluation.evaluatee_faculty
        if not faculty:
            continue

        section_groups = defaultdict(list)
        detailed_answers = defaultdict(list)

        for response in evaluation.responses.all():
            section_key = (response.section_code or "").strip()
            section_name = (response.section_name or "").strip() or "Unnamed Section"

            if section_key:
                section_groups[section_key].append(response.rating)

            detailed_answers[section_name].append({
                "question_number": response.question_number,
                "question_text": response.question_text or f"Question {response.question_number}",
                "rating": response.rating,
            })

        evaluator_sections = {}
        for section_key, ratings in section_groups.items():
            if ratings:
                evaluator_sections[section_key] = round(sum(ratings) / len(ratings), 2)

        evaluator_average_score = round(float(evaluation.average_score or 0), 2)
        evaluator_total_score = round(float(evaluation.total_score or 0), 2)
        evaluator_computed_rating = round((evaluator_total_score / 75) * 100, 2) if evaluator_total_score else 0

        grouped[faculty.id]["faculty"] = faculty
        grouped[faculty.id]["evaluators"].append({
            "name": evaluation.evaluator_name or "Unknown Evaluator",
            "department": evaluation.evaluator_department or "",
            "average_score": evaluator_average_score,
            "total_score": evaluator_total_score,
            "computed_rating": evaluator_computed_rating,
            "submitted_at": evaluation.submitted_at,
            "comments": evaluation.comments or "",
            "sections": evaluator_sections,
            "detailed_answers": dict(detailed_answers),
        })

        grouped[faculty.id]["overall_values"].append(evaluator_average_score)
        grouped[faculty.id]["total_scores"].append(evaluator_total_score)
        grouped[faculty.id]["computed_ratings"].append(evaluator_computed_rating)

        for section_key, value in evaluator_sections.items():
            grouped[faculty.id]["section_values"][section_key].append(value)

    results = []
    completed_count = 0

    for index, faculty in enumerate(faculty_members, start=1):
        item = grouped.get(faculty.id)
        evaluators = item["evaluators"] if item else []
        evaluator_count = len(evaluators)

        average_score = round(sum(item["overall_values"]) / evaluator_count, 2) if item and evaluator_count else 0
        average_total_score = round(sum(item["total_scores"]) / evaluator_count, 2) if item and evaluator_count else 0
        computed_rating = round(sum(item["computed_ratings"]) / evaluator_count, 2) if item and evaluator_count else 0

        section_averages = {
            "management_teaching_learning": 0,
            "content_knowledge_pedagogy_technology": 0,
            "commitment_transparency": 0,
        }

        if item:
            for section_key, values in item["section_values"].items():
                section_averages[section_key] = round(sum(values) / len(values), 2) if values else 0

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
            "sections": section_averages,
            "evaluators": evaluators,
        })

    total_instructors = len(faculty_members)
    pending_reviews = max(total_instructors - completed_count, 0)

    rated_results = [r for r in results if r["evaluator_count"] > 0]
    dept_avg_score = round(
        sum(r["computed_rating"] for r in rated_results) / len(rated_results),
        2
    ) if rated_results else 0

    return {
        "results": results,
        "total_instructors": total_instructors,
        "evaluations_complete": completed_count,
        "pending_reviews": pending_reviews,
        "dept_avg_score": dept_avg_score,
    }


def head_add(request):
    logged_in_head = _get_logged_in_head(request)
    if not logged_in_head:
        messages.error(request, "Please log in first.")
        return redirect("admin_login")

    open_schedule = _get_open_schedule()
    if not open_schedule:
        messages.error(request, "No open evaluation schedule.")
        return redirect("head_monitor")

    faculty_qs = (
        FacultyMember.objects
        .filter(schedule=open_schedule, department=logged_in_head.department)
        .order_by("name")
    )

    if request.method == "POST":
        uploaded_file = request.FILES.get("faculty_file")

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
                        schedule=open_schedule,
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
        "open_schedule": open_schedule,
    }

    return render(request, "head/head_add.html", context)


def head_monitor(request):
    logged_in_head = _get_logged_in_head(request)
    if not logged_in_head:
        messages.error(request, "Please log in first.")
        return redirect("admin_login")

    selected_schedule = _get_latest_schedule_for_head_dashboard(logged_in_head)

    summary = {
        "results": [],
        "total_instructors": 0,
        "evaluations_complete": 0,
        "pending_reviews": 0,
        "dept_avg_score": 0,
    }

    if selected_schedule:
        summary = _build_head_results_for_schedule(logged_in_head, selected_schedule)

    context = {
        "logged_in_head": logged_in_head,
        "open_schedule": _get_open_schedule(),
        "selected_schedule": selected_schedule,
        **summary,
    }
    return render(request, "head/head_monitor.html", context)


def head_past_evaluations(request):
    logged_in_head = _get_logged_in_head(request)
    if not logged_in_head:
        messages.error(request, "Please log in first.")
        return redirect("admin_login")

    selected_schedule_id = request.GET.get("schedule")
    now = timezone.localtime(timezone.now())

    past_schedules = (
        EvaluationSchedule.objects
        .filter(
            end_datetime__lt=now,
            faculty_members__department=logged_in_head.department,
        )
        .distinct()
        .order_by("-start_datetime", "-created_at")
    )

    selected_schedule = None
    if selected_schedule_id:
        selected_schedule = past_schedules.filter(id=selected_schedule_id).first()

    if not selected_schedule:
        selected_schedule = past_schedules.first()

    summary = {
        "results": [],
        "total_instructors": 0,
        "evaluations_complete": 0,
        "pending_reviews": 0,
        "dept_avg_score": 0,
    }

    if selected_schedule:
        summary = _build_head_results_for_schedule(logged_in_head, selected_schedule)

    context = {
        "logged_in_head": logged_in_head,
        "selected_schedule": selected_schedule,
        "past_schedules": past_schedules,
        **summary,
    }
    return render(request, "head/head_past_evaluations.html", context)


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
        .select_related("department", "schedule")
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