from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook
from io import TextIOWrapper
from datetime import datetime
import csv
from collections import defaultdict
from django.db.models import Prefetch, Case, When, IntegerField
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.signing import TimestampSigner
from django.template.loader import render_to_string
from django.urls import reverse

from ..models import (
    Department,
    FacultyMember,
    DepartmentHead,
    EvaluationSchedule,
    FacultyEvaluation,
    FacultyEvaluationResponse,
    HeadEvaluation,
    HeadEvaluationResponse,
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


DEPARTMENT_MAP = {
    "DED": "Department of Industrial Education",
    "DIT": "Department of Industrial Technology",
    "DLA": "Department of Liberal Arts",
    "DOE": "Department of Engineering",
    "DMS": "Department of Math and Science",
}


def _admin_context(active_page, extra=None):
    context = {"active_page": active_page}
    if extra:
        context.update(extra)
    return context


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


def _replace_faculty_from_department_sheet(ws, department, schedule):
    """
    Expected sheet format:
    NAME | GSFE EMAIL
    """
    FacultyMember.objects.filter(schedule=schedule, department=department).delete()

    faculty_to_create = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        name = str(row[0]).strip() if row and len(row) > 0 and row[0] else ""
        email = str(row[1]).strip() if row and len(row) > 1 and row[1] else ""

        if not name:
            continue

        faculty_to_create.append(
            FacultyMember(
                schedule=schedule,
                department=department,
                id_number="",
                name=name,
                email=email,
            )
        )

    FacultyMember.objects.bulk_create(faculty_to_create)
    return len(faculty_to_create)


def _replace_faculty_from_uploaded_file(uploaded_file, department, schedule):
    """
    Supports:
    - .xlsx
    - .csv

    Accepted columns:
    - NAME, GSFE EMAIL
    - ID NUMBER, NAME, GSFE EMAIL
    - NAME, EMAIL
    """
    file_name = uploaded_file.name.lower()

    FacultyMember.objects.filter(schedule=schedule, department=department).delete()
    created_count = 0

    if file_name.endswith(".xlsx"):
        wb = load_workbook(uploaded_file, data_only=True)
        ws = wb.active

        headers = [
            str(cell.value).strip().upper() if cell.value else ""
            for cell in ws[1]
        ]

        if "NAME" not in headers:
            return 0

        faculty_to_create = []

        name_index = headers.index("NAME")
        id_index = headers.index("ID NUMBER") if "ID NUMBER" in headers else None

        if "GSFE EMAIL" in headers:
            email_index = headers.index("GSFE EMAIL")
        elif "EMAIL" in headers:
            email_index = headers.index("EMAIL")
        else:
            email_index = None

        for row in ws.iter_rows(min_row=2, values_only=True):
            id_number = str(row[id_index]).strip() if id_index is not None and row[id_index] else ""
            name = str(row[name_index]).strip() if row[name_index] else ""
            email = str(row[email_index]).strip() if email_index is not None and row[email_index] else ""

            if not name:
                continue

            faculty_to_create.append(
                FacultyMember(
                    schedule=schedule,
                    department=department,
                    id_number=id_number,
                    name=name,
                    email=email,
                )
            )

        FacultyMember.objects.bulk_create(faculty_to_create)
        created_count = len(faculty_to_create)

    elif file_name.endswith(".csv"):
        decoded_file = TextIOWrapper(uploaded_file.file, encoding="utf-8")
        reader = csv.DictReader(decoded_file)

        faculty_to_create = []

        for row in reader:
            normalized_row = {
                str(k).strip().upper(): (str(v).strip() if v else "")
                for k, v in row.items()
            }

            id_number = normalized_row.get("ID NUMBER", "")
            name = normalized_row.get("NAME", "")
            email = normalized_row.get("GSFE EMAIL", "") or normalized_row.get("EMAIL", "")

            if not name:
                continue

            faculty_to_create.append(
                FacultyMember(
                    schedule=schedule,
                    department=department,
                    id_number=id_number,
                    name=name,
                    email=email,
                )
            )

        FacultyMember.objects.bulk_create(faculty_to_create)
        created_count = len(faculty_to_create)

    return created_count


def _parse_datetime_local(value):
    if not value:
        return None
    naive_dt = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    return timezone.make_aware(naive_dt, timezone.get_current_timezone())


def _get_latest_schedule_with_uploaded_data():
    schedules = (
        EvaluationSchedule.objects
        .order_by("-start_datetime", "-created_at")
    )

    for schedule in schedules:
        has_faculty = FacultyMember.objects.filter(schedule=schedule).exists()
        has_heads = DepartmentHead.objects.filter(schedule=schedule).exists()

        if has_faculty or has_heads:
            return schedule

    return None


def _get_latest_schedule_with_submitted_evaluations():
    schedules = (
        EvaluationSchedule.objects
        .order_by("-start_datetime", "-created_at")
    )

    for schedule in schedules:
        has_faculty_eval = FacultyEvaluation.objects.filter(
            schedule=schedule,
            status="submitted"
        ).exists()
        has_head_eval = HeadEvaluation.objects.filter(
            schedule=schedule,
            status="submitted"
        ).exists()

        if has_faculty_eval or has_head_eval:
            return schedule

    return None


def admin_department(request):
    if request.method == "POST" and request.FILES.get("excel_file"):
        schedule_id = request.POST.get("schedule_id")
        if not schedule_id:
            messages.error(request, "Please select an evaluation schedule.")
            return redirect("admin_department")

        selected_schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)
        excel_file = request.FILES["excel_file"]

        try:
            wb = load_workbook(excel_file, data_only=True)
        except Exception:
            messages.error(request, "Invalid Excel file. Please upload a valid .xlsx workbook.")
            return redirect("admin_department")

        imported_faculty = 0
        imported_heads = 0

        with transaction.atomic():
            for sheet_name in wb.sheetnames:
                code = str(sheet_name).strip().upper()

                if code == "HEAD":
                    continue

                if code not in DEPARTMENT_MAP:
                    continue

                ws = wb[sheet_name]

                department, _ = Department.objects.get_or_create(
                    code=code,
                    defaults={"name": DEPARTMENT_MAP[code]},
                )

                department.name = DEPARTMENT_MAP[code]
                department.save()

                imported_faculty += _replace_faculty_from_department_sheet(
                    ws, department, selected_schedule
                )

            if "HEAD" in wb.sheetnames:
                ws = wb["HEAD"]

                for row in ws.iter_rows(min_row=2, values_only=True):
                    head_name = str(row[0]).strip() if row and len(row) > 0 and row[0] else ""
                    head_email = str(row[1]).strip() if row and len(row) > 1 and row[1] else ""
                    dept_value = str(row[2]).strip() if row and len(row) > 2 and row[2] else ""

                    if not head_name or not dept_value:
                        continue

                    dept_key = dept_value.upper()

                    if dept_key in DEPARTMENT_MAP:
                        dept_code = dept_key
                        dept_name = DEPARTMENT_MAP[dept_key]
                    else:
                        matched_code = None
                        for code, full_name in DEPARTMENT_MAP.items():
                            if dept_value.lower() == full_name.lower():
                                matched_code = code
                                break

                        if matched_code:
                            dept_code = matched_code
                            dept_name = DEPARTMENT_MAP[matched_code]
                        else:
                            dept_code = dept_value.upper().replace(" ", "_")
                            dept_name = dept_value

                    department, _ = Department.objects.get_or_create(
                        code=dept_code,
                        defaults={"name": dept_name},
                    )

                    department.name = dept_name
                    department.save()

                    DepartmentHead.objects.update_or_create(
                        schedule=selected_schedule,
                        department=department,
                        defaults={
                            "name": head_name,
                            "email": head_email,
                        },
                    )

                    imported_heads += 1

        messages.success(
            request,
            f"Import complete: {imported_faculty} faculty and {imported_heads} department heads processed.",
        )
        return redirect("admin_department")

    schedules = EvaluationSchedule.objects.all().order_by("-start_datetime", "-created_at")
    selected_schedule_id = request.GET.get("schedule")

    display_schedule = None
    if selected_schedule_id:
        display_schedule = schedules.filter(id=selected_schedule_id).first()

    if not display_schedule:
        display_schedule = _get_open_schedule()

    if not display_schedule:
        display_schedule = _get_latest_schedule_with_uploaded_data()

    if not display_schedule:
        display_schedule = schedules.first()

    if display_schedule:
        departments = Department.objects.prefetch_related(
            Prefetch(
                "faculty_members",
                queryset=FacultyMember.objects.filter(schedule=display_schedule).order_by("name"),
            ),
            Prefetch(
                "heads",
                queryset=DepartmentHead.objects.filter(schedule=display_schedule).order_by("name"),
            ),
        ).order_by("name")

        total_faculty = FacultyMember.objects.filter(schedule=display_schedule).count()
    else:
        departments = Department.objects.prefetch_related(
            Prefetch("faculty_members", queryset=FacultyMember.objects.none()),
            Prefetch("heads", queryset=DepartmentHead.objects.none()),
        ).order_by("name")
        total_faculty = 0

    context = _admin_context(
        "department",
        {
            "departments": departments,
            "schedules": schedules,
            "total_departments": departments.count(),
            "total_faculty": total_faculty,
            "latest_department": departments.last(),
            "current_schedule": display_schedule,
        },
    )
    return render(request, "admin/admin_department.html", context)


def admin_manage(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save_schedule":
            schedule_id = request.POST.get("schedule_id")
            title = (request.POST.get("title") or "").strip()
            academic_year = (request.POST.get("academic_year") or "").strip()
            semester = (request.POST.get("semester") or "").strip()
            notes = (request.POST.get("notes") or "").strip()
            start_raw = request.POST.get("start_datetime")
            end_raw = request.POST.get("end_datetime")

            start_datetime = _parse_datetime_local(start_raw)
            end_datetime = _parse_datetime_local(end_raw)

            if not all([title, academic_year, semester, start_datetime, end_datetime]):
                messages.error(request, "Please complete all required schedule fields.")
                return redirect("admin_manage")

            if end_datetime <= start_datetime:
                messages.error(request, "Closing date and time must be later than the opening date and time.")
                return redirect("admin_manage")

            if schedule_id:
                schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)
                schedule.title = title
                schedule.academic_year = academic_year
                schedule.semester = semester
                schedule.start_datetime = start_datetime
                schedule.end_datetime = end_datetime
                schedule.notes = notes
                schedule.save()
                messages.success(request, "Evaluation schedule updated successfully.")
            else:
                EvaluationSchedule.objects.create(
                    title=title,
                    academic_year=academic_year,
                    semester=semester,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    notes=notes,
                )
                messages.success(request, "Evaluation schedule created successfully.")

            return redirect("admin_manage")

        elif action == "delete_schedule":
            schedule_id = request.POST.get("schedule_id")
            schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)
            schedule.delete()
            messages.success(request, "Evaluation schedule deleted successfully.")
            return redirect("admin_manage")

    schedules = EvaluationSchedule.objects.all()
    now = timezone.localtime(timezone.now())

    context = _admin_context(
        "manage",
        {
            "schedules": schedules,
            "total_periods": schedules.count(),
            "open_periods": sum(1 for s in schedules if s.computed_status == "Open"),
            "closed_periods": sum(1 for s in schedules if s.computed_status == "Closed"),
            "current_time": now,
        },
    )
    return render(request, "admin/admin_manage.html", context)


def add_department(request):
    if request.method != "POST":
        return redirect("admin_department")

    code = (request.POST.get("code") or "").strip().upper()
    name = (request.POST.get("name") or "").strip()
    head_name = (request.POST.get("head_name") or "").strip()
    head_email = (request.POST.get("head_email") or "").strip()
    faculty_file = request.FILES.get("faculty_file")
    schedule_id = request.POST.get("schedule_id")

    if not schedule_id:
        messages.error(request, "Please select an evaluation schedule.")
        return redirect("admin_department")

    selected_schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)

    if not code or not name:
        messages.error(request, "Department code and name are required.")
        return redirect("admin_department")

    department, created = Department.objects.get_or_create(
        code=code,
        defaults={"name": name},
    )

    if not created:
        messages.error(request, f"Department code '{code}' already exists.")
        return redirect("admin_department")

    department.name = name
    department.save()

    if head_name:
        DepartmentHead.objects.update_or_create(
            schedule=selected_schedule,
            department=department,
            defaults={
                "name": head_name,
                "email": head_email or "",
            },
        )

    if faculty_file:
        try:
            count = _replace_faculty_from_uploaded_file(
                faculty_file, department, selected_schedule
            )
            messages.success(request, f"Department added successfully with {count} faculty members.")
        except Exception as e:
            messages.warning(request, f"Department added, but faculty file could not be processed: {str(e)}")
            return redirect("admin_department")
    else:
        messages.success(request, "Department added successfully.")

    return redirect("admin_department")


def update_department(request, dept_id):
    if request.method != "POST":
        return redirect("admin_department")

    department = get_object_or_404(Department, id=dept_id)

    code = (request.POST.get("code") or "").strip().upper()
    name = (request.POST.get("name") or "").strip()
    head_name = (request.POST.get("head_name") or "").strip()
    head_email = (request.POST.get("head_email") or "").strip()
    faculty_file = request.FILES.get("faculty_file")
    schedule_id = request.POST.get("schedule_id")

    if not schedule_id:
        messages.error(request, "Please select an evaluation schedule.")
        return redirect("admin_department")

    selected_schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)

    if not code or not name:
        messages.error(request, "Department code and name are required.")
        return redirect("admin_department")

    existing_department = Department.objects.filter(code=code).exclude(id=department.id).first()
    if existing_department:
        messages.error(request, f"Department code '{code}' is already used by another department.")
        return redirect("admin_department")

    department.code = code
    department.name = name
    department.save()

    if head_name:
        DepartmentHead.objects.update_or_create(
            schedule=selected_schedule,
            department=department,
            defaults={
                "name": head_name,
                "email": head_email or "",
            },
        )
    else:
        DepartmentHead.objects.filter(schedule=selected_schedule, department=department).delete()

    if faculty_file:
        try:
            count = _replace_faculty_from_uploaded_file(
                faculty_file, department, selected_schedule
            )
            messages.success(request, f"Department updated successfully. Faculty list replaced with {count} records.")
        except Exception as e:
            messages.warning(request, f"Department updated, but faculty file could not be processed: {str(e)}")
            return redirect("admin_department")
    else:
        messages.success(request, "Department updated successfully.")

    return redirect("admin_department")


def delete_department(request, dept_id):
    if request.method != "POST":
        return redirect("admin_department")

    department = get_object_or_404(Department, id=dept_id)
    department_name = department.name
    department.delete()

    messages.success(request, f"Department '{department_name}' was deleted successfully.")
    return redirect("admin_department")


def admin_results_summary(request):
    schedules = EvaluationSchedule.objects.all().order_by("-start_datetime", "-created_at")
    selected_schedule_id = request.GET.get("schedule")

    selected_schedule = None
    if selected_schedule_id:
        selected_schedule = schedules.filter(id=selected_schedule_id).first()

    if not selected_schedule:
        selected_schedule = _get_latest_schedule_with_submitted_evaluations()

    if not selected_schedule:
        selected_schedule = _get_latest_schedule_with_uploaded_data()

    results = []

    if not selected_schedule:
        context = _admin_context(
            "results_summary",
            {
                "faculty_results": [],
                "departments": [],
                "total_faculty_count": 0,
                "highest_average_grade": 0,
                "lowest_average_grade": 0,
                "overall_faculty_average": 0,
                "selected_schedule": None,
                "academic_years": [],
                "semesters": [],
                "selected_academic_year": "",
                "selected_semester": "",
                "schedules": schedules,
            },
        )
        return render(request, "admin/admin_overall.html", context)

    faculty_evaluations = (
        FacultyEvaluation.objects
        .filter(status="submitted", schedule=selected_schedule)
        .select_related(
            "evaluatee_faculty__department",
            "evaluator_head__department",
            "schedule",
        )
        .prefetch_related(
            Prefetch(
                "responses",
                queryset=_ordered_response_queryset(FacultyEvaluationResponse),
            )
        )
        .order_by("evaluatee_name", "evaluator_name", "submitted_at")
    )

    head_evaluations = (
        HeadEvaluation.objects
        .filter(status="submitted", schedule=selected_schedule)
        .select_related(
            "evaluatee_head__department",
            "evaluator_head__department",
            "schedule",
        )
        .prefetch_related(
            Prefetch(
                "responses",
                queryset=_ordered_response_queryset(HeadEvaluationResponse),
            )
        )
        .order_by("evaluatee_name", "evaluator_name", "submitted_at")
    )

    grouped_results = {}

    def add_evaluation_to_group(
        grouped,
        result_type,
        schedule_obj,
        target_id,
        target_name,
        target_department,
        evaluator_name,
        evaluator_department,
        average_score,
        total_score,
        comments,
        submitted_at,
        responses,
    ):
        schedule_label = ""
        schedule_key = "no-schedule"

        if schedule_obj:
            schedule_key = str(schedule_obj.id)
            schedule_label = f"{schedule_obj.academic_year} | {schedule_obj.semester} | {schedule_obj.title}"

        group_key = f"{result_type}-{schedule_key}-{target_id}"

        if group_key not in grouped:
            grouped[group_key] = {
                "id": target_id,
                "result_type": result_type,
                "name": target_name,
                "department": target_department,
                "schedule_id": schedule_obj.id if schedule_obj else None,
                "schedule_label": schedule_label,
                "academic_year": schedule_obj.academic_year if schedule_obj else "",
                "semester": schedule_obj.semester if schedule_obj else "",
                "title": schedule_obj.title if schedule_obj else "",
                "evaluators": [],
                "section_values": defaultdict(list),
                "total_scores": [],
                "computed_ratings": [],
                "overall_values": [],
            }

        section_groups = defaultdict(list)
        detailed_answers = defaultdict(list)

        for response in responses:
            section_key = (response.section_code or "").strip()
            section_name = (response.section_name or "").strip() or "Unnamed Section"

            if section_key:
                section_groups[section_key].append(float(response.rating or 0))

            detailed_answers[section_name].append({
                "question_number": response.question_number,
                "question_text": response.question_text or f"Question {response.question_number}",
                "rating": float(response.rating or 0),
            })

        evaluator_sections = {}
        for section_key, ratings in section_groups.items():
            if ratings:
                evaluator_sections[section_key] = round(sum(ratings) / len(ratings), 2)

        evaluator_overall = round(float(average_score or 0), 2)
        evaluator_total_score = round(float(total_score or 0), 2)
        evaluator_computed_rating = round((evaluator_total_score / 75) * 100, 2) if evaluator_total_score else 0

        grouped[group_key]["evaluators"].append({
            "evaluator_name": evaluator_name or "Unknown Evaluator",
            "evaluator_department": evaluator_department or "",
            "sections": evaluator_sections,
            "overall": evaluator_overall,
            "total_score": evaluator_total_score,
            "computed_rating": evaluator_computed_rating,
            "comments": comments or "",
            "submitted_at": submitted_at.strftime("%Y-%m-%d %H:%M") if submitted_at else "",
            "detailed_answers": dict(detailed_answers),
        })

        grouped[group_key]["total_scores"].append(evaluator_total_score)
        grouped[group_key]["computed_ratings"].append(evaluator_computed_rating)
        grouped[group_key]["overall_values"].append(evaluator_overall)

        for section_key, value in evaluator_sections.items():
            grouped[group_key]["section_values"][section_key].append(value)

    for evaluation in faculty_evaluations:
        target_id = evaluation.evaluatee_faculty.id if evaluation.evaluatee_faculty else f"faculty-eval-{evaluation.id}"
        target_name = (
            evaluation.evaluatee_name
            or (evaluation.evaluatee_faculty.name if evaluation.evaluatee_faculty else "Unknown Faculty")
        )
        target_department = (
            evaluation.evaluatee_department
            or (
                evaluation.evaluatee_faculty.department.name
                if evaluation.evaluatee_faculty and evaluation.evaluatee_faculty.department
                else ""
            )
        )

        add_evaluation_to_group(
            grouped=grouped_results,
            result_type="faculty",
            schedule_obj=evaluation.schedule,
            target_id=target_id,
            target_name=target_name,
            target_department=target_department,
            evaluator_name=evaluation.evaluator_name,
            evaluator_department=evaluation.evaluator_department,
            average_score=evaluation.average_score,
            total_score=evaluation.total_score,
            comments=evaluation.comments,
            submitted_at=evaluation.submitted_at,
            responses=evaluation.responses.all(),
        )

    for evaluation in head_evaluations:
        target_id = evaluation.evaluatee_head.id if evaluation.evaluatee_head else f"head-eval-{evaluation.id}"
        target_name = (
            evaluation.evaluatee_name
            or (evaluation.evaluatee_head.name if evaluation.evaluatee_head else "Unknown Department Head")
        )
        target_department = (
            evaluation.evaluatee_department
            or (
                evaluation.evaluatee_head.department.name
                if evaluation.evaluatee_head and evaluation.evaluatee_head.department
                else ""
            )
        )

        add_evaluation_to_group(
            grouped=grouped_results,
            result_type="head",
            schedule_obj=evaluation.schedule,
            target_id=target_id,
            target_name=target_name,
            target_department=target_department,
            evaluator_name=evaluation.evaluator_name,
            evaluator_department=evaluation.evaluator_department,
            average_score=evaluation.average_score,
            total_score=evaluation.total_score,
            comments=evaluation.comments,
            submitted_at=evaluation.submitted_at,
            responses=evaluation.responses.all(),
        )

    for _, item in grouped_results.items():
        section_averages = {}
        for section_key, values in item["section_values"].items():
            section_averages[section_key] = round(sum(values) / len(values), 2) if values else 0

        overall_average = round(sum(item["overall_values"]) / len(item["overall_values"]), 2) if item["overall_values"] else 0
        average_total_score = round(sum(item["total_scores"]) / len(item["total_scores"]), 2) if item["total_scores"] else 0
        computed_rating = round(sum(item["computed_ratings"]) / len(item["computed_ratings"]), 2) if item["computed_ratings"] else 0

        results.append({
            "id": item["id"],
            "result_type": item["result_type"],
            "name": item["name"],
            "department": item["department"],
            "schedule_id": item["schedule_id"],
            "schedule_label": item["schedule_label"],
            "academic_year": item["academic_year"],
            "semester": item["semester"],
            "title": item["title"],
            "sections": section_averages,
            "overall": overall_average,
            "average_total_score": average_total_score,
            "computed_rating": computed_rating,
            "evaluator_count": len(item["evaluators"]),
            "evaluators": item["evaluators"],
        })

    results.sort(key=lambda x: (x["result_type"], str(x["name"]).lower()))
    overall_list = [r["overall"] for r in results if r["overall"] > 0]

    departments = list(
        Department.objects
        .filter(faculty_members__schedule=selected_schedule)
        .order_by("name")
        .values_list("name", flat=True)
        .distinct()
    )

    head_departments = list(
        Department.objects
        .filter(heads__schedule=selected_schedule)
        .order_by("name")
        .values_list("name", flat=True)
        .distinct()
    )

    departments = sorted(set(departments + head_departments))

    context = _admin_context(
        "results_summary",
        {
            "faculty_results": results,
            "departments": departments,
            "total_faculty_count": len(results),
            "highest_average_grade": round(max(overall_list), 2) if overall_list else 0,
            "lowest_average_grade": round(min(overall_list), 2) if overall_list else 0,
            "overall_faculty_average": round(sum(overall_list) / len(overall_list), 2) if overall_list else 0,
            "selected_schedule": selected_schedule,
            "academic_years": [],
            "semesters": [],
            "selected_academic_year": "",
            "selected_semester": "",
            "schedules": schedules,
        },
    )

    return render(request, "admin/admin_overall.html", context)


def admin_overall(request):
    return admin_results_summary(request)


def admin_login(request):
    open_schedule = _get_open_schedule()
    portal_closed = open_schedule is None

    if request.method == "POST":
        login_type = (request.POST.get("login_type") or "").strip()

        if login_type == "admin":
            username = (request.POST.get("username") or "").strip()
            password = (request.POST.get("password") or "").strip()

            if not username or not password:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "Please enter both username and password for admin login."
                }
                return redirect("admin_login")

            return redirect("admin_department")

        elif login_type == "head":
            email = (request.POST.get("email") or "").strip().lower()

            if not email:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "Please enter your GSFE email address."
                }
                return redirect("admin_login")

            head = (
                DepartmentHead.objects
                .select_related("department", "schedule")
                .filter(email__iexact=email)
                .order_by("-schedule__start_datetime", "-id")
                .first()
            )

            faculty = (
                FacultyMember.objects
                .select_related("department", "schedule")
                .filter(email__iexact=email)
                .order_by("-schedule__start_datetime", "-id")
                .first()
            )

            if not head:
                if faculty:
                    request.session["login_modal"] = {
                        "type": "danger",
                        "message": "This account is registered as faculty only. Faculty members are not allowed to access the department head portal."
                    }
                else:
                    request.session["login_modal"] = {
                        "type": "danger",
                        "message": "This email is not registered as a department head in the evaluation system."
                    }
                return redirect("admin_login")

            signer = TimestampSigner(salt=LINK_SALT)
            token = signer.sign(str(head.id))

            verify_url = request.build_absolute_uri(
                reverse("verify_head_login_link", args=[token])
            )

            subject = "Department Head Portal Login Link"

            context = {
                "head": head,
                "verify_url": verify_url,
                "expires_minutes": LOGIN_LINK_MAX_AGE // 60,
                "open_schedule": open_schedule,
            }

            text_body = (
                f"Hello {head.name},\n\n"
                f"Click the link below to access the Department Head Portal:\n\n"
                f"{verify_url}\n\n"
                f"This link will expire in {LOGIN_LINK_MAX_AGE // 60} minutes.\n"
                f"If you did not request this, please ignore this email."
            )

            html_body = render_to_string("head/email_head_portal_link.html", context)

            try:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=text_body,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    to=[head.email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send()

                request.session["login_modal"] = {
                    "type": "success",
                    "message": f"A secure login link has been sent to {head.email}."
                }
            except Exception:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "The login link could not be sent. Please check your email settings."
                }

            return redirect("admin_login")

            if not head:
                if faculty:
                    request.session["login_modal"] = {
                        "type": "danger",
                        "message": "This account is registered as faculty only. Faculty members are not allowed to access the department head portal."
                    }
                else:
                    request.session["login_modal"] = {
                        "type": "danger",
                        "message": "This email is not registered as a department head in the evaluation system."
                    }
                return redirect("admin_login")

            signer = TimestampSigner(salt=LINK_SALT)
            token = signer.sign(str(head.id))

            verify_url = request.build_absolute_uri(
                reverse("verify_head_login_link", args=[token])
            )

            subject = "Department Head Portal Login Link"

            context = {
                "head": head,
                "verify_url": verify_url,
                "expires_minutes": LOGIN_LINK_MAX_AGE // 60,
                "open_schedule": open_schedule,
            }

            text_body = (
                f"Hello {head.name},\n\n"
                f"Click the link below to access the Department Head Portal:\n\n"
                f"{verify_url}\n\n"
                f"This link will expire in {LOGIN_LINK_MAX_AGE // 60} minutes.\n"
                f"If you did not request this, please ignore this email."
            )

            html_body = render_to_string("head/email_head_portal_link.html", context)

            try:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=text_body,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    to=[head.email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send()

                request.session["login_modal"] = {
                    "type": "success",
                    "message": f"A secure login link has been sent to {head.email}."
                }
            except Exception:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "The login link could not be sent. Please check your email settings."
                }

            return redirect("admin_login")

        else:
            request.session["login_modal"] = {
                "type": "danger",
                "message": "Please choose a login type first."
            }
            return redirect("admin_login")

    login_modal = request.session.pop("login_modal", None)

    context = _admin_context(
        "login",
        {
            "login_modal": login_modal,
            "open_schedule": open_schedule,
            "portal_closed": portal_closed,
        },
    )
    return render(request, "admin/admin_login.html", context)

def admin_past_evaluations(request):
    selected_schedule_id = request.GET.get("schedule")

    past_schedules = (
        EvaluationSchedule.objects
        .filter(end_datetime__lt=timezone.localtime(timezone.now()))
        .order_by("-start_datetime", "-created_at")
    )

    selected_schedule = None
    if selected_schedule_id:
        selected_schedule = past_schedules.filter(id=selected_schedule_id).first()

    if not selected_schedule:
        selected_schedule = past_schedules.first()

    faculty_history_results = []
    head_history_results = []
    history_results = []

    if selected_schedule:
        faculty_evaluations = (
            FacultyEvaluation.objects
            .filter(schedule=selected_schedule, status="submitted")
            .select_related(
                "evaluatee_faculty__department",
                "evaluator_head__department",
                "schedule",
            )
            .prefetch_related(
                Prefetch(
                    "responses",
                    queryset=_ordered_response_queryset(FacultyEvaluationResponse),
                )
            )
            .order_by("evaluatee_name", "evaluator_name", "submitted_at")
        )

        head_evaluations = (
            HeadEvaluation.objects
            .filter(schedule=selected_schedule, status="submitted")
            .select_related(
                "evaluatee_head__department",
                "evaluator_head__department",
                "schedule",
            )
            .prefetch_related(
                Prefetch(
                    "responses",
                    queryset=_ordered_response_queryset(HeadEvaluationResponse),
                )
            )
            .order_by("evaluatee_name", "evaluator_name", "submitted_at")
        )

        grouped_faculty = {}
        grouped_heads = {}

        def add_to_group(
            grouped,
            result_type,
            schedule_obj,
            group_key,
            target_id,
            target_name,
            target_department,
            evaluation,
        ):
            schedule_label = ""
            schedule_id_value = None
            academic_year = ""
            semester = ""
            title = ""

            if schedule_obj:
                schedule_id_value = schedule_obj.id
                academic_year = schedule_obj.academic_year or ""
                semester = schedule_obj.semester or ""
                title = schedule_obj.title or ""
                schedule_label = f"{academic_year} | {semester} | {title}"

            if group_key not in grouped:
                grouped[group_key] = {
                    "id": target_id,
                    "result_type": result_type,
                    "name": target_name,
                    "department": target_department,
                    "schedule_id": schedule_id_value,
                    "schedule_label": schedule_label,
                    "academic_year": academic_year,
                    "semester": semester,
                    "title": title,
                    "evaluators": [],
                    "section_values": defaultdict(list),
                    "overall_values": [],
                    "total_scores": [],
                    "computed_ratings": [],
                }

            section_groups = defaultdict(list)
            detailed_answers = defaultdict(list)

            for response in evaluation.responses.all():
                section_key = (response.section_code or "").strip()
                section_name = (response.section_name or "").strip() or "Unnamed Section"
                rating_value = float(response.rating or 0)

                if section_key:
                    section_groups[section_key].append(rating_value)

                detailed_answers[section_name].append({
                    "question_number": response.question_number,
                    "question_text": response.question_text or f"Question {response.question_number}",
                    "rating": rating_value,
                })

            evaluator_sections = {}
            for section_key, ratings in section_groups.items():
                if ratings:
                    evaluator_sections[section_key] = round(sum(ratings) / len(ratings), 2)

            evaluator_total_score = round(float(evaluation.total_score or 0), 2)
            evaluator_overall = round(float(evaluation.average_score or 0), 2)
            evaluator_computed_rating = round((evaluator_total_score / 75) * 100, 2) if evaluator_total_score else 0

            grouped[group_key]["evaluators"].append({
                "evaluator_name": evaluation.evaluator_name or "Unknown Evaluator",
                "evaluator_department": evaluation.evaluator_department or "",
                "sections": evaluator_sections,
                "overall": evaluator_overall,
                "total_score": evaluator_total_score,
                "computed_rating": evaluator_computed_rating,
                "comments": evaluation.comments or "",
                "submitted_at": evaluation.submitted_at.strftime("%Y-%m-%d %H:%M") if evaluation.submitted_at else "",
                "detailed_answers": dict(detailed_answers),
            })

            grouped[group_key]["overall_values"].append(evaluator_overall)
            grouped[group_key]["total_scores"].append(evaluator_total_score)
            grouped[group_key]["computed_ratings"].append(evaluator_computed_rating)

            for section_key, value in evaluator_sections.items():
                grouped[group_key]["section_values"][section_key].append(value)

        for evaluation in faculty_evaluations:
            target_id = evaluation.evaluatee_faculty.id if evaluation.evaluatee_faculty else f"faculty-eval-{evaluation.id}"
            target_name = (
                evaluation.evaluatee_name
                or (evaluation.evaluatee_faculty.name if evaluation.evaluatee_faculty else "Unknown Faculty")
            )
            target_department = (
                evaluation.evaluatee_department
                or (
                    evaluation.evaluatee_faculty.department.name
                    if evaluation.evaluatee_faculty and evaluation.evaluatee_faculty.department
                    else ""
                )
            )

            add_to_group(
                grouped=grouped_faculty,
                result_type="faculty",
                schedule_obj=evaluation.schedule,
                group_key=f"faculty-{selected_schedule.id}-{target_id}",
                target_id=target_id,
                target_name=target_name,
                target_department=target_department,
                evaluation=evaluation,
            )

        for evaluation in head_evaluations:
            target_id = evaluation.evaluatee_head.id if evaluation.evaluatee_head else f"head-eval-{evaluation.id}"
            target_name = (
                evaluation.evaluatee_name
                or (evaluation.evaluatee_head.name if evaluation.evaluatee_head else "Unknown Department Head")
            )
            target_department = (
                evaluation.evaluatee_department
                or (
                    evaluation.evaluatee_head.department.name
                    if evaluation.evaluatee_head and evaluation.evaluatee_head.department
                    else ""
                )
            )

            add_to_group(
                grouped=grouped_heads,
                result_type="head",
                schedule_obj=evaluation.schedule,
                group_key=f"head-{selected_schedule.id}-{target_id}",
                target_id=target_id,
                target_name=target_name,
                target_department=target_department,
                evaluation=evaluation,
            )

        for _, item in grouped_faculty.items():
            section_averages = {}
            for section_key, values in item["section_values"].items():
                section_averages[section_key] = round(sum(values) / len(values), 2) if values else 0

            overall_average = round(sum(item["overall_values"]) / len(item["overall_values"]), 2) if item["overall_values"] else 0
            average_total_score = round(sum(item["total_scores"]) / len(item["total_scores"]), 2) if item["total_scores"] else 0
            computed_rating = round(sum(item["computed_ratings"]) / len(item["computed_ratings"]), 2) if item["computed_ratings"] else 0

            faculty_history_results.append({
                "id": item["id"],
                "result_type": item["result_type"],
                "name": item["name"],
                "department": item["department"],
                "schedule_id": item["schedule_id"],
                "schedule_label": item["schedule_label"],
                "academic_year": item["academic_year"],
                "semester": item["semester"],
                "title": item["title"],
                "sections": section_averages,
                "overall": overall_average,
                "average_total_score": average_total_score,
                "computed_rating": computed_rating,
                "evaluator_count": len(item["evaluators"]),
                "evaluators": item["evaluators"],
            })

        for _, item in grouped_heads.items():
            section_averages = {}
            for section_key, values in item["section_values"].items():
                section_averages[section_key] = round(sum(values) / len(values), 2) if values else 0

            overall_average = round(sum(item["overall_values"]) / len(item["overall_values"]), 2) if item["overall_values"] else 0
            average_total_score = round(sum(item["total_scores"]) / len(item["total_scores"]), 2) if item["total_scores"] else 0
            computed_rating = round(sum(item["computed_ratings"]) / len(item["computed_ratings"]), 2) if item["computed_ratings"] else 0

            head_history_results.append({
                "id": item["id"],
                "result_type": item["result_type"],
                "name": item["name"],
                "department": item["department"],
                "schedule_id": item["schedule_id"],
                "schedule_label": item["schedule_label"],
                "academic_year": item["academic_year"],
                "semester": item["semester"],
                "title": item["title"],
                "sections": section_averages,
                "overall": overall_average,
                "average_total_score": average_total_score,
                "computed_rating": computed_rating,
                "evaluator_count": len(item["evaluators"]),
                "evaluators": item["evaluators"],
            })

        faculty_history_results.sort(key=lambda x: str(x["name"]).lower())
        head_history_results.sort(key=lambda x: str(x["name"]).lower())

        history_results = faculty_history_results + head_history_results
        history_results.sort(key=lambda x: (x["result_type"], str(x["name"]).lower()))

    context = _admin_context("past_evaluations", {
        "past_schedules": past_schedules,
        "selected_schedule": selected_schedule,
        "faculty_history_results": faculty_history_results,
        "head_history_results": head_history_results,
        "history_results": history_results,
        "faculty_count": len(faculty_history_results),
        "head_count": len(head_history_results),
        "total_count": len(history_results),
    })

    return render(request, "admin/admin_past_evaluations.html", context)