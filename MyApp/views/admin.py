from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook
from io import TextIOWrapper
from datetime import datetime
import csv

from ..models import Department, FacultyMember, DepartmentHead, EvaluationSchedule


DEPARTMENT_MAP = {
    "DED": "Department of Industrial Education",
    "DIT": "Department of Industrial Technology",
    "DLA": "Department of Liberal Arts",
    "DOE": "Department of Engineering",
    "DMS": "Department of Math and Science",
}


# =========================
# HELPERS
# =========================

def _admin_context(active_page, extra=None):
    context = {"active_page": active_page}
    if extra:
        context.update(extra)
    return context


def _replace_faculty_from_department_sheet(ws, department):
    """
    Expected sheet format:
    NAME | GSFE EMAIL
    """
    FacultyMember.objects.filter(department=department).delete()

    faculty_to_create = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        name = str(row[0]).strip() if row and len(row) > 0 and row[0] else ""
        email = str(row[1]).strip() if row and len(row) > 1 and row[1] else ""

        if not name:
            continue

        faculty_to_create.append(
            FacultyMember(
                department=department,
                id_number="",
                name=name,
                email=email,
            )
        )

    FacultyMember.objects.bulk_create(faculty_to_create)
    return len(faculty_to_create)


def _replace_faculty_from_uploaded_file(uploaded_file, department):
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

    FacultyMember.objects.filter(department=department).delete()
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
            normalized_row = {str(k).strip().upper(): (str(v).strip() if v else "") for k, v in row.items()}

            id_number = normalized_row.get("ID NUMBER", "")
            name = normalized_row.get("NAME", "")
            email = normalized_row.get("GSFE EMAIL", "") or normalized_row.get("EMAIL", "")

            if not name:
                continue

            faculty_to_create.append(
                FacultyMember(
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
    """
    Parses datetime-local input like: 2026-03-25T08:30
    """
    if not value:
        return None
    naive_dt = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    return timezone.make_aware(naive_dt, timezone.get_current_timezone())


# =========================
# BASIC ADMIN PAGES
# =========================

def admin_login(request):
    return render(
        request,
        "admin/admin_login.html",
        _admin_context("login"),
    )


def admin_department(request):
    if request.method == "POST" and request.FILES.get("excel_file"):
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

                imported_faculty += _replace_faculty_from_department_sheet(ws, department)

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

                    DepartmentHead.objects.update_or_create(
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

    departments = Department.objects.prefetch_related("faculty_members", "head").order_by("name")

    context = _admin_context(
        "department",
        {
            "departments": departments,
            "total_departments": departments.count(),
            "total_faculty": sum(dept.faculty_members.count() for dept in departments),
            "latest_department": departments.last(),
        },
    )
    return render(request, "admin/admin_department.html", context)


def admin_results_summary(request):
    return render(
        request,
        "admin/admin_overall.html",
        _admin_context("results_summary"),
    )


def admin_overall(request):
    return render(
        request,
        "admin/admin_overall.html",
        _admin_context("results_summary"),
    )


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


# =========================
# ADD DEPARTMENT
# =========================

def add_department(request):
    if request.method != "POST":
        return redirect("admin_department")

    code = (request.POST.get("code") or "").strip().upper()
    name = (request.POST.get("name") or "").strip()
    head_name = (request.POST.get("head_name") or "").strip()
    head_email = (request.POST.get("head_email") or "").strip()
    faculty_file = request.FILES.get("faculty_file")

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
            department=department,
            defaults={
                "name": head_name,
                "email": head_email or "",
            },
        )

    if faculty_file:
        try:
            count = _replace_faculty_from_uploaded_file(faculty_file, department)
            messages.success(request, f"Department added successfully with {count} faculty members.")
        except Exception as e:
            messages.warning(request, f"Department added, but faculty file could not be processed: {str(e)}")
            return redirect("admin_department")
    else:
        messages.success(request, "Department added successfully.")

    return redirect("admin_department")


# =========================
# UPDATE DEPARTMENT
# =========================

def update_department(request, dept_id):
    if request.method != "POST":
        return redirect("admin_department")

    department = get_object_or_404(Department, id=dept_id)

    code = (request.POST.get("code") or "").strip().upper()
    name = (request.POST.get("name") or "").strip()
    head_name = (request.POST.get("head_name") or "").strip()
    head_email = (request.POST.get("head_email") or "").strip()
    faculty_file = request.FILES.get("faculty_file")

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
            department=department,
            defaults={
                "name": head_name,
                "email": head_email or "",
            },
        )
    else:
        DepartmentHead.objects.filter(department=department).delete()

    if faculty_file:
        try:
            count = _replace_faculty_from_uploaded_file(faculty_file, department)
            messages.success(request, f"Department updated successfully. Faculty list replaced with {count} records.")
        except Exception as e:
            messages.warning(request, f"Department updated, but faculty file could not be processed: {str(e)}")
            return redirect("admin_department")
    else:
        messages.success(request, "Department updated successfully.")

    return redirect("admin_department")


# =========================
# DELETE DEPARTMENT
# =========================

def delete_department(request, dept_id):
    if request.method != "POST":
        return redirect("admin_department")

    department = get_object_or_404(Department, id=dept_id)
    department_name = department.name
    department.delete()

    messages.success(request, f"Department '{department_name}' was deleted successfully.")
    return redirect("admin_department")