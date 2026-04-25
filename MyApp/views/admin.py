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
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils.text import slugify



from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from ..models import (
    Department,
    FacultyMember,
    DepartmentHead,
    EvaluationOfficer,
    EvaluationSchedule,
    FacultyEvaluation,
    FacultyEvaluationResponse,
    OfficeEvaluation,
    OfficeEvaluationResponse,
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
    Supports department sheets with flexible headers:
    - NAME | GSFE EMAIL
    - ID NUMBER | NAME | GSFE EMAIL
    - NAME | EMAIL
    - FIRST NAME | LAST NAME | EMAIL
    """

    FacultyMember.objects.filter(schedule=schedule, department=department).delete()

    headers = [
        str(cell.value).strip().upper() if cell.value else ""
        for cell in ws[1]
    ]

    def get_index(*possible_names):
        for name in possible_names:
            if name in headers:
                return headers.index(name)
        return None

    name_index = get_index("NAME", "FULL NAME", "FACULTY NAME")
    first_name_index = get_index("FIRST NAME", "FIRSTNAME")
    last_name_index = get_index("LAST NAME", "LASTNAME")
    id_index = get_index("ID NUMBER", "ID NO", "ID", "EMPLOYEE ID")
    email_index = get_index("GSFE EMAIL", "EMAIL", "EMAIL ADDRESS")

    faculty_to_create = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        id_number = ""
        name = ""
        email = ""

        if id_index is not None and id_index < len(row) and row[id_index]:
            id_number = str(row[id_index]).strip()

        if name_index is not None and name_index < len(row) and row[name_index]:
            name = str(row[name_index]).strip()

        if not name:
            first_name = ""
            last_name = ""

            if first_name_index is not None and first_name_index < len(row) and row[first_name_index]:
                first_name = str(row[first_name_index]).strip()

            if last_name_index is not None and last_name_index < len(row) and row[last_name_index]:
                last_name = str(row[last_name_index]).strip()

            name = f"{first_name} {last_name}".strip()

        if email_index is not None and email_index < len(row) and row[email_index]:
            email = str(row[email_index]).strip()

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
    schedules = EvaluationSchedule.objects.order_by("-start_datetime", "-created_at")

    for schedule in schedules:
        has_faculty_eval = FacultyEvaluation.objects.filter(
            schedule=schedule,
            status="submitted"
        ).exists()

        has_office_eval = OfficeEvaluation.objects.filter(
            schedule=schedule,
            status="submitted"
        ).exists()

        has_head_eval = HeadEvaluation.objects.filter(
            schedule=schedule,
            status="submitted"
        ).exists()

        if has_faculty_eval or has_office_eval or has_head_eval:
            return schedule

    return None


def admin_required(view_func):
    return user_passes_test(
        lambda u: u.is_authenticated and u.is_staff,
        login_url='admin_login'
    )(view_func)


@admin_required
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

                # skip non-department sheets
                if code in ["HEAD", "OFFICES"]:
                    continue

                ws = wb[sheet_name]

                # If the sheet is one of the original departments, use the fixed full name.
                # If it is a newly added department sheet, use the sheet name as the department name.
                department_name = DEPARTMENT_MAP.get(code, str(sheet_name).strip())

                department, _ = Department.objects.get_or_create(
                    code=code,
                    defaults={"name": department_name},
                )

                department.name = department_name
                department.save()

                imported_faculty += _replace_faculty_from_department_sheet(
                    ws, department, selected_schedule
                )

            if "OFFICES" in wb.sheetnames:
                ws = wb["OFFICES"]

                EvaluationOfficer.objects.filter(schedule=selected_schedule).delete()

                for row in ws.iter_rows(min_row=2, values_only=True):
                    role = str(row[0]).strip().upper() if row and len(row) > 0 and row[0] else ""
                    officer_name = str(row[1]).strip() if row and len(row) > 1 and row[1] else ""
                    officer_email = str(row[2]).strip() if row and len(row) > 2 and row[2] else ""

                    if not role or not officer_name or not officer_email:
                        continue

                    if role not in ["OCD", "ADAA"]:
                        continue

                    EvaluationOfficer.objects.update_or_create(
                        schedule=selected_schedule,
                        role=role,
                        defaults={
                            "name": officer_name,
                            "email": officer_email,
                        },
                    )
                    
                    
            # import HEAD
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


@admin_required
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

            # exact duplicate check
            existing = EvaluationSchedule.objects.filter(
                title=title,
                academic_year=academic_year,
                semester=semester,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
            )

            if schedule_id:
                existing = existing.exclude(id=schedule_id)

            if existing.exists():
                messages.error(
                    request,
                    "This schedule already exists. Please create another schedule with different details."
                )
                return redirect("admin_manage")

            # ONLY ONE SCHEDULE TOTAL IN THE SYSTEM
            now = timezone.localtime(timezone.now())

            existing_schedule = EvaluationSchedule.objects.filter(
                start_datetime__lte=now,
                end_datetime__gte=now
            )

            if schedule_id:
                existing_schedule = existing_schedule.exclude(id=schedule_id)

            if existing_schedule.exists():
                messages.error(
                    request,
                    "Only one evaluation schedule is allowed. Wait for it to finish before creating a new one."
                )
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
                return redirect("admin_manage")
            else:
                schedule = EvaluationSchedule.objects.create(
                    title=title,
                    academic_year=academic_year,
                    semester=semester,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    notes=notes,
                )
                messages.success(request, "Evaluation schedule created successfully.")
                return redirect(f"{reverse('admin_department')}?schedule={schedule.id}")

        elif action == "delete_schedule":
            schedule_id = request.POST.get("schedule_id")
            schedule = get_object_or_404(EvaluationSchedule, id=schedule_id)
            schedule.delete()
            messages.success(request, "Evaluation schedule deleted successfully.")
            return redirect("admin_manage")

    schedules = EvaluationSchedule.objects.all().order_by("-start_datetime", "-created_at")
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


@admin_required
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
        return redirect(f"{reverse('admin_department')}?schedule={selected_schedule.id}")

    department, created = Department.objects.get_or_create(
        code=code,
        defaults={"name": name},
    )

    # If the department already exists, update its name instead of blocking it.
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
                faculty_file,
                department,
                selected_schedule
            )

            if created:
                messages.success(request, f"Department added successfully with {count} faculty members.")
            else:
                messages.success(request, f"Department already existed. Faculty list for this schedule was updated with {count} records.")

        except Exception as e:
            messages.warning(request, f"Department saved, but faculty file could not be processed: {str(e)}")
            return redirect(f"{reverse('admin_department')}?schedule={selected_schedule.id}")
    else:
        if created:
            messages.success(request, "Department added successfully.")
        else:
            messages.success(request, "Department already existed. Department details were updated for the selected schedule.")

    return redirect(f"{reverse('admin_department')}?schedule={selected_schedule.id}")

@admin_required
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
    redirect_url = f"{reverse('admin_department')}?schedule={selected_schedule.id}"

    if not code or not name:
        messages.error(request, "Department code and name are required.")
        return redirect(redirect_url)

    existing_department = Department.objects.filter(code=code).exclude(id=department.id).first()
    if existing_department:
        messages.error(request, f"Department code '{code}' is already used by another department.")
        return redirect(redirect_url)

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
        DepartmentHead.objects.filter(
            schedule=selected_schedule,
            department=department
        ).delete()

    if faculty_file:
        try:
            count = _replace_faculty_from_uploaded_file(
                faculty_file,
                department,
                selected_schedule
            )
            messages.success(
                request,
                f"Department updated successfully. Faculty list replaced with {count} records."
            )
        except Exception as e:
            messages.warning(
                request,
                f"Department updated, but faculty file could not be processed: {str(e)}"
            )
            return redirect(redirect_url)
    else:
        messages.success(request, "Department updated successfully.")

    return redirect(redirect_url)


def delete_department(request, dept_id):
    if request.method != "POST":
        return redirect("admin_department")

    department = get_object_or_404(Department, id=dept_id)
    department_name = department.name
    department.delete()

    messages.success(request, f"Department '{department_name}' was deleted successfully.")
    return redirect("admin_department")

@admin_required
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
                "schedule_label": schedule_label,
                "academic_year": schedule_obj.academic_year if schedule_obj else "",
                "semester": schedule_obj.semester if schedule_obj else "",
                "title": schedule_obj.title if schedule_obj else "",
                "evaluators": [],
                "section_values": defaultdict(list),
                "overall_values": [],
                "total_scores": [],
                "computed_ratings": [],
            }

        section_groups = defaultdict(list)
        detailed_answers = defaultdict(list)

        for response in responses:
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

        evaluator_sections = {
            "management_teaching_learning": 0,
            "content_knowledge_pedagogy_technology": 0,
            "commitment_transparency": 0,
        }

        for section_key, values in section_groups.items():
            evaluator_sections[section_key] = (sum(values) / len(values)) if values else 0
            grouped[group_key]["section_values"][section_key].append(evaluator_sections[section_key])

        evaluator_average_score = float(average_score or 0)
        evaluator_total_score = float(total_score or 0)
        evaluator_computed_rating = ((evaluator_total_score / 75) * 100) if evaluator_total_score else 0

        grouped[group_key]["evaluators"].append({
            "evaluator_name": evaluator_name or "Unknown Evaluator",
            "evaluator_department": evaluator_department or "",
            "average_score": evaluator_average_score,
            "total_score": evaluator_total_score,
            "computed_rating": evaluator_computed_rating,
            "submitted_at": submitted_at,
            "comments": comments or "",
            "sections": evaluator_sections,
            "detailed_answers": dict(detailed_answers),
        })

        grouped[group_key]["overall_values"].append(evaluator_average_score)
        grouped[group_key]["total_scores"].append(evaluator_total_score)
        grouped[group_key]["computed_ratings"].append(evaluator_computed_rating)

    # =========================
    # FACULTY RESULTS
    # =========================
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

    for evaluation in faculty_evaluations:
        add_evaluation_to_group(
            grouped=grouped_results,
            result_type="faculty",
            schedule_obj=evaluation.schedule,
            target_id=evaluation.evaluatee_faculty_id,
            target_name=evaluation.evaluatee_name,
            target_department=evaluation.evaluatee_department,
            evaluator_name=evaluation.evaluator_name,
            evaluator_department=evaluation.evaluator_department,
            average_score=evaluation.average_score,
            total_score=evaluation.total_score,
            comments=evaluation.comments,
            submitted_at=evaluation.submitted_at,
            responses=evaluation.responses.all(),
        )

    # =========================
    # HEAD RESULTS (ADAA -> Heads)
    # =========================
    head_evaluations = (
        HeadEvaluation.objects
        .filter(status="submitted", schedule=selected_schedule)
        .select_related(
            "evaluatee_head__department",
            "evaluator_officer",
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

    for evaluation in head_evaluations:
        add_evaluation_to_group(
            grouped=grouped_results,
            result_type="head",
            schedule_obj=evaluation.schedule,
            target_id=evaluation.evaluatee_head_id,
            target_name=evaluation.evaluatee_name,
            target_department=evaluation.evaluatee_department or (
                evaluation.evaluatee_head.department.name if evaluation.evaluatee_head and evaluation.evaluatee_head.department else ""
            ),
            evaluator_name=evaluation.evaluator_name,
            evaluator_department="Office of the ADAA",
            average_score=evaluation.average_score,
            total_score=evaluation.total_score,
            comments=evaluation.comments,
            submitted_at=evaluation.submitted_at,
            responses=evaluation.responses.all(),
        )

    # =========================
    # OFFICE RESULTS (OCD -> ADAA)
    # =========================
    office_evaluations = (
        OfficeEvaluation.objects
        .filter(status="submitted", schedule=selected_schedule)
        .select_related(
            "evaluatee_officer",
            "evaluator_officer",
            "schedule",
        )
        .prefetch_related(
            Prefetch(
                "responses",
                queryset=_ordered_response_queryset(OfficeEvaluationResponse),
            )
        )
        .order_by("evaluatee_name", "evaluator_name", "submitted_at")
    )

    for evaluation in office_evaluations:
        target_department = (
            "Office of the ADAA"
            if (evaluation.evaluatee_role or "").upper() == "ADAA"
            else "Office of the Campus Director"
        )

        evaluator_department = (
            "Office of the Campus Director"
            if (evaluation.evaluator_role or "").upper() == "OCD"
            else "Office of the ADAA"
        )

        add_evaluation_to_group(
            grouped=grouped_results,
            result_type="office",
            schedule_obj=evaluation.schedule,
            target_id=evaluation.evaluatee_officer_id,
            target_name=evaluation.evaluatee_name,
            target_department=target_department,
            evaluator_name=evaluation.evaluator_name,
            evaluator_department=evaluator_department,
            average_score=evaluation.average_score,
            total_score=evaluation.total_score,
            comments=evaluation.comments,
            submitted_at=evaluation.submitted_at,
            responses=evaluation.responses.all(),
        )

    overall_list = []
    results = []

    for index, group in enumerate(grouped_results.values(), start=1):
        evaluator_count = len(group["evaluators"])

        average_score = (sum(group["overall_values"]) / evaluator_count) if evaluator_count else 0
        average_total_score = (sum(group["total_scores"]) / evaluator_count) if evaluator_count else 0
        computed_rating = (sum(group["computed_ratings"]) / evaluator_count) if evaluator_count else 0

        section_averages = {
            "management_teaching_learning": 0,
            "content_knowledge_pedagogy_technology": 0,
            "commitment_transparency": 0,
        }

        for section_key, values in group["section_values"].items():
            section_averages[section_key] = (sum(values) / len(values)) if values else 0

        overall_list.append(computed_rating)

        results.append({
            "num": index,
            "id": group["id"],
            "result_type": group["result_type"],
            "name": group["name"],
            "department": group["department"],
            "position": (
                "Faculty Member" if group["result_type"] == "faculty"
                else "Department Head" if group["result_type"] == "head"
                else "Evaluation Officer"
            ),
            "evaluator_count": evaluator_count,
            "average_score": average_score,
            "average_total_score": average_total_score,
            "computed_rating": computed_rating,
            "sections": section_averages,
            "evaluators": group["evaluators"],
            "academic_year": group["academic_year"],
            "semester": group["semester"],
            "title": group["title"],
            "schedule_label": group["schedule_label"],
        })

    type_order = {"office": 0, "head": 1, "faculty": 2}
    results.sort(key=lambda item: (type_order.get(item["result_type"], 99), item["name"].lower()))

    departments = sorted({item["department"] for item in results if item["department"]})

    context = _admin_context(
        "results_summary",
        {
            "faculty_results": results,
            "departments": departments,
            "total_faculty_count": len(results),
            "highest_average_grade": max(overall_list) if overall_list else 0,
            "lowest_average_grade": min(overall_list) if overall_list else 0,
            "overall_faculty_average": (sum(overall_list) / len(overall_list)) if overall_list else 0,
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

            user = authenticate(request, username=username, password=password)

            if user is None:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "Invalid username or password."
                }
                return redirect("admin_login")

            if not user.is_active:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "This account is inactive."
                }
                return redirect("admin_login")

            if not user.is_staff:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "You do not have admin access."
                }
                return redirect("admin_login")

            login(request, user)
            return redirect("admin_manage")

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
                "recipient_name": head.name,
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

def admin_forgot_password(request):
    if request.method != "POST":
        return redirect("admin_login")

    email = (request.POST.get("forgot_email") or "").strip().lower()

    if not email:
        request.session["login_modal"] = {
            "type": "danger",
            "message": "Please enter your admin email address."
        }
        return redirect("admin_login")

    user = (
        User.objects
        .filter(email__iexact=email, is_staff=True, is_active=True)
        .order_by("id")
        .first()
    )

    if not user:
        request.session["login_modal"] = {
            "type": "danger",
            "message": "No active admin account is registered with that email address."
        }
        return redirect("admin_login")

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    reset_url = request.build_absolute_uri(
        reverse("admin_reset_password", args=[uid, token])
    )

    subject = "Admin Password Reset Request"

    text_body = (
        f"Hello {user.username},\n\n"
        f"You requested to reset your admin password.\n\n"
        f"Click the link below to continue:\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, please ignore this email."
    )

    html_body = f"""
        <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #1f2937;">
            <h2 style="color: #981b2e;">Admin Password Reset</h2>
            <p>Hello <strong>{user.username}</strong>,</p>
            <p>You requested to reset your admin password.</p>
            <p>
                <a href="{reset_url}"
                   style="display:inline-block;padding:12px 20px;background:#981b2e;color:#fff;text-decoration:none;border-radius:8px;font-weight:700;">
                   Reset Password
                </a>
            </p>
            <p>If the button does not work, copy and paste this link into your browser:</p>
            <p>{reset_url}</p>
            <p>If you did not request this, you may ignore this email.</p>
        </div>
    """

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, "EMAIL_HOST_USER", None),
            to=[user.email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()

        request.session["login_modal"] = {
            "type": "success",
            "message": f"A password reset link has been sent to {user.email}."
        }
    except Exception:
        request.session["login_modal"] = {
            "type": "danger",
            "message": "Password reset email could not be sent. Please check your email settings."
        }

    return redirect("admin_login")


def admin_reset_password(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid, is_staff=True)
    except Exception:
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "admin/admin_reset_password.html", {
            "validlink": False
        })

    if request.method == "POST":
        password1 = (request.POST.get("password1") or "").strip()
        password2 = (request.POST.get("password2") or "").strip()

        if not password1 or not password2:
            return render(request, "admin/admin_reset_password.html", {
                "validlink": True,
                "error_message": "Please fill in both password fields."
            })

        if password1 != password2:
            return render(request, "admin/admin_reset_password.html", {
                "validlink": True,
                "error_message": "Passwords do not match."
            })

        try:
            validate_password(password1, user=user)
        except ValidationError as e:
            return render(request, "admin/admin_reset_password.html", {
                "validlink": True,
                "error_message": " ".join(e.messages)
            })

        user.set_password(password1)
        user.save()

        request.session["login_modal"] = {
            "type": "success",
            "message": "Your password has been reset successfully. You may now log in."
        }
        return redirect("admin_login")

    return render(request, "admin/admin_reset_password.html", {
        "validlink": True
    })


@admin_required
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
    history_results = []

    if selected_schedule:
        grouped_results = {}

        def add_to_group(
            grouped,
            result_type,
            schedule_obj,
            group_key,
            target_id,
            target_name,
            target_department,
            evaluation,
            responses,
            evaluator_name,
            evaluator_department,
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

            for response in responses:
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

            evaluator_sections = {
                "management_teaching_learning": 0,
                "content_knowledge_pedagogy_technology": 0,
                "commitment_transparency": 0,
            }

            for section_key, ratings in section_groups.items():
                evaluator_sections[section_key] = round(sum(ratings) / len(ratings), 2) if ratings else 0
                grouped[group_key]["section_values"][section_key].append(evaluator_sections[section_key])

            evaluator_total_score = round(float(evaluation.total_score or 0), 2)
            evaluator_overall = round(float(evaluation.average_score or 0), 2)
            evaluator_computed_rating = round((evaluator_total_score / 75) * 100, 2) if evaluator_total_score else 0

            grouped[group_key]["evaluators"].append({
                "evaluator_name": evaluator_name or "Unknown Evaluator",
                "evaluator_department": evaluator_department or "",
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

        # =========================
        # FACULTY RESULTS
        # =========================
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
                grouped=grouped_results,
                result_type="faculty",
                schedule_obj=evaluation.schedule,
                group_key=f"faculty-{selected_schedule.id}-{target_id}",
                target_id=target_id,
                target_name=target_name,
                target_department=target_department,
                evaluation=evaluation,
                responses=evaluation.responses.all(),
                evaluator_name=evaluation.evaluator_name,
                evaluator_department=evaluation.evaluator_department,
            )

        # =========================
        # HEAD RESULTS
        # =========================
        head_evaluations = (
            HeadEvaluation.objects
            .filter(schedule=selected_schedule, status="submitted")
            .select_related(
                "evaluatee_head__department",
                "evaluator_officer",
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
                grouped=grouped_results,
                result_type="head",
                schedule_obj=evaluation.schedule,
                group_key=f"head-{selected_schedule.id}-{target_id}",
                target_id=target_id,
                target_name=target_name,
                target_department=target_department,
                evaluation=evaluation,
                responses=evaluation.responses.all(),
                evaluator_name=evaluation.evaluator_name,
                evaluator_department="Office of the ADAA",
            )

        # =========================
        # OFFICE RESULTS
        # =========================
        office_evaluations = (
            OfficeEvaluation.objects
            .filter(schedule=selected_schedule, status="submitted")
            .select_related(
                "evaluatee_officer",
                "evaluator_officer",
                "schedule",
            )
            .prefetch_related(
                Prefetch(
                    "responses",
                    queryset=_ordered_response_queryset(OfficeEvaluationResponse),
                )
            )
            .order_by("evaluatee_name", "evaluator_name", "submitted_at")
        )

        for evaluation in office_evaluations:
            target_id = evaluation.evaluatee_officer.id if evaluation.evaluatee_officer else f"office-eval-{evaluation.id}"
            target_name = evaluation.evaluatee_name or (
                evaluation.evaluatee_officer.name if evaluation.evaluatee_officer else "Unknown Officer"
            )

            target_department = (
                "Office of the ADAA"
                if (evaluation.evaluatee_role or "").upper() == "ADAA"
                else "Office of the Campus Director"
            )

            evaluator_department = (
                "Office of the Campus Director"
                if (evaluation.evaluator_role or "").upper() == "OCD"
                else "Office of the ADAA"
            )

            add_to_group(
                grouped=grouped_results,
                result_type="office",
                schedule_obj=evaluation.schedule,
                group_key=f"office-{selected_schedule.id}-{target_id}",
                target_id=target_id,
                target_name=target_name,
                target_department=target_department,
                evaluation=evaluation,
                responses=evaluation.responses.all(),
                evaluator_name=evaluation.evaluator_name,
                evaluator_department=evaluator_department,
            )

        all_history_results = []

        for _, item in grouped_results.items():
            section_averages = {
                "management_teaching_learning": 0,
                "content_knowledge_pedagogy_technology": 0,
                "commitment_transparency": 0,
            }

            for section_key, values in item["section_values"].items():
                section_averages[section_key] = round(sum(values) / len(values), 2) if values else 0

            overall_average = round(sum(item["overall_values"]) / len(item["overall_values"]), 2) if item["overall_values"] else 0
            average_total_score = round(sum(item["total_scores"]) / len(item["total_scores"]), 2) if item["total_scores"] else 0
            computed_rating = round(sum(item["computed_ratings"]) / len(item["computed_ratings"]), 2) if item["computed_ratings"] else 0

            all_history_results.append({
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

        type_order = {"office": 0, "head": 1, "faculty": 2}
        all_history_results.sort(key=lambda x: (type_order.get(x["result_type"], 99), str(x["name"]).lower()))

        faculty_history_results = [item for item in all_history_results if item["result_type"] == "faculty"]
        history_results = all_history_results

    context = _admin_context("past_evaluations", {
        "past_schedules": past_schedules,
        "selected_schedule": selected_schedule,
        "faculty_history_results": faculty_history_results,
        "history_results": history_results,
        "faculty_count": len(faculty_history_results),
        "total_count": len(history_results),
    })

    return render(request, "admin/admin_past_evaluations.html", context)

def admin_logout(request):
    logout(request)
    return redirect("admin_login")

@login_required
def admin_pending(request):
    schedules = EvaluationSchedule.objects.order_by('-start_datetime')
    selected_schedule = schedules.first()

    total_pending_evaluatees = 0
    total_done_evaluatees = 0
    department_tabs = []

    total_pending_heads = 0
    total_done_heads = 0
    total_heads = 0
    head_pending_rows = []
    head_done_rows = []

    if selected_schedule:
        # ==============================
        # HEAD MONITORING: ADAA -> HEADS
        # ==============================
        department_heads_for_adaa = (
            DepartmentHead.objects
            .filter(schedule=selected_schedule)
            .select_related("department")
            .order_by("department__name", "name")
        )

        for head in department_heads_for_adaa:
            total_heads += 1

            evaluation = (
                HeadEvaluation.objects
                .filter(
                    schedule=selected_schedule,
                    evaluatee_head=head
                )
                .order_by("-submitted_at", "-id")
                .first()
            )

            department_name = head.department.name if head.department else "No Department"
            department_code = head.department.code if head.department else "—"

            if evaluation and evaluation.status == "submitted":
                head_done_rows.append({
                    "head_name": evaluation.evaluatee_name or head.name,
                    "department": evaluation.evaluatee_department or department_name,
                    "department_code": department_code,
                    "status": "Done",
                    "evaluator_name": evaluation.evaluator_name or "ADAA",
                    "submitted_at": evaluation.submitted_at.strftime('%b %d, %Y %I:%M %p') if evaluation.submitted_at else "—",
                })
                total_done_heads += 1
            else:
                head_pending_rows.append({
                    "head_name": head.name,
                    "department": department_name,
                    "department_code": department_code,
                    "status": "Not Yet Finished",
                })
                total_pending_heads += 1

        # ===================================
        # FACULTY MONITORING: HEAD -> FACULTY
        # ===================================
        department_heads = (
            DepartmentHead.objects
            .filter(schedule=selected_schedule)
            .select_related('department')
            .order_by('department__name', 'name')
        )

        departments_map = {}

        for head in department_heads:
            dept = head.department
            dept_name = dept.name if dept else "No Department"
            dept_code = getattr(dept, 'code', '') if dept else ''

            if dept_name not in departments_map:
                departments_map[dept_name] = {
                    'name': dept_name,
                    'code': dept_code,
                    'slug': slugify(dept_name) or f"department-{len(departments_map)+1}",
                    'pending_rows': [],
                    'done_rows': [],
                    'pending_count': 0,
                    'done_count': 0,
                    'total_faculty': 0,
                }

            faculty_members = (
                FacultyMember.objects
                .filter(
                    schedule=selected_schedule,
                    department=dept
                )
                .select_related('department')
                .order_by('name')
            )

            for faculty in faculty_members:
                evaluation = (
                    FacultyEvaluation.objects
                    .filter(
                        schedule=selected_schedule,
                        evaluator_head=head,
                        evaluatee_faculty=faculty
                    )
                    .first()
                )

                if evaluation and evaluation.status == 'submitted':
                    departments_map[dept_name]['done_rows'].append({
                        'evaluatee_name': evaluation.evaluatee_name or faculty.name,
                        'evaluatee_department': evaluation.evaluatee_department or dept_name,
                        'status': 'Done',
                        'submitted_at': evaluation.submitted_at.strftime('%b %d, %Y %I:%M %p') if evaluation.submitted_at else '—',
                    })
                    departments_map[dept_name]['done_count'] += 1
                    total_done_evaluatees += 1
                else:
                    departments_map[dept_name]['pending_rows'].append({
                        'evaluatee_name': faculty.name,
                        'evaluatee_department': dept_name,
                        'status': 'Not Yet Finished',
                    })
                    departments_map[dept_name]['pending_count'] += 1
                    total_pending_evaluatees += 1

        for dept_name, dept_data in departments_map.items():
            dept_data['total_faculty'] = dept_data['pending_count'] + dept_data['done_count']

        department_tabs = sorted(departments_map.values(), key=lambda x: x['name'])

    context = {
        'active_page': 'pending',
        'selected_schedule': selected_schedule,
        'schedules': schedules,

        # Faculty pending data
        'department_tabs': department_tabs,
        'total_pending_evaluatees': total_pending_evaluatees,
        'total_done_evaluatees': total_done_evaluatees,

        # Head pending data
        'total_heads': total_heads,
        'total_pending_heads': total_pending_heads,
        'total_done_heads': total_done_heads,
        'head_pending_rows': head_pending_rows,
        'head_done_rows': head_done_rows,
    }

    return render(request, 'admin/admin_pending.html', context)