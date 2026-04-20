import json
from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMultiAlternatives
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    EvaluationSchedule,
    DepartmentHead,
    EvaluationOfficer,
    FacultyMember,
    FacultyEvaluation,
    FacultyEvaluationResponse,
    OfficeEvaluation,
    OfficeEvaluationResponse,
    HeadEvaluation,
    HeadEvaluationResponse,
)

LOGIN_LINK_MAX_AGE = 300  # 5 minutes
LINK_SALT = "faculty-eval-login"


def get_open_schedule():
    now = timezone.localtime(timezone.now())
    return (
        EvaluationSchedule.objects
        .filter(start_datetime__lte=now, end_datetime__gte=now)
        .order_by("start_datetime")
        .first()
    )


def _build_saved_state_for_head(schedule, logged_in_head):
    faculty_saved = []

    faculty_evaluations = (
        FacultyEvaluation.objects
        .filter(schedule=schedule, evaluator_head=logged_in_head)
        .select_related("evaluatee_faculty")
        .prefetch_related("responses")
    )

    for evaluation in faculty_evaluations:
        response_map = {}
        for response in evaluation.responses.all().order_by("section_name", "question_number"):
            section_key = (response.section_name or "").strip()
            if not section_key:
                continue
            response_map.setdefault(section_key, {})[str(response.question_number - 1)] = response.rating

        faculty_saved.append({
            "evaluatee_id": str(evaluation.evaluatee_faculty_id),
            "comments": evaluation.comments or "",
            "status": evaluation.status or "submitted",
            "answers": response_map,
        })

    return {
        "faculty": faculty_saved,
    }

def _build_dashboard_summary(schedule, logged_in_head, department_faculty_members):
    faculty_saved_count = FacultyEvaluation.objects.filter(
        schedule=schedule,
        evaluator_head=logged_in_head,
        status="submitted"
    ).count()

    faculty_required_count = (
        department_faculty_members.count()
        if hasattr(department_faculty_members, "count")
        else len(department_faculty_members)
    )

    return {
        "faculty": {
            "saved": faculty_saved_count,
            "required": faculty_required_count,
            "status": (
                "submitted"
                if faculty_required_count and faculty_saved_count == faculty_required_count
                else ("in_progress" if faculty_saved_count > 0 else "not_started")
            ),
        },
        "overall": {
            "saved": faculty_saved_count,
            "required": faculty_required_count,
        }
    }


def eval_login(request):
    open_schedule = get_open_schedule()
    portal_closed = open_schedule is None

    if request.method == "POST":
        action = (request.POST.get("action") or "send_link").strip()
        email = (request.POST.get("email") or "").strip().lower()

        if portal_closed:
            request.session["login_modal"] = {
                "type": "warning",
                "message": "The evaluation portal is currently closed. Please wait for the next evaluation schedule."
            }
            return redirect("eval_login")

        if action != "send_link":
            action = "send_link"

        if not email:
            request.session["login_modal"] = {
                "type": "danger",
                "message": "Please enter your email address."
            }
            return redirect("eval_login")

        officer = (
            EvaluationOfficer.objects
            .filter(schedule=open_schedule, email__iexact=email)
            .first()
        )

        head = (
            DepartmentHead.objects
            .select_related("department")
            .filter(schedule=open_schedule, email__iexact=email)
            .first()
        )

        faculty = (
            FacultyMember.objects
            .select_related("department")
            .filter(schedule=open_schedule, email__iexact=email)
            .first()
        )

        if not officer and not head:
            if faculty:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "This account is registered as faculty only. Faculty members are not allowed to access the evaluation portal."
                }
            else:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "This email is not registered in the evaluation system."
                }
            return redirect("eval_login")

        signer = TimestampSigner(salt=LINK_SALT)

        if officer:
            token_value = f"officer:{officer.id}"
            recipient_name = officer.name
            recipient_email = officer.email
        else:
            token_value = f"head:{head.id}"
            recipient_name = head.name
            recipient_email = head.email

        token = signer.sign(token_value)

        verify_url = request.build_absolute_uri(
            reverse("verify_login_link", args=[token])
        )

        subject = "Faculty Evaluation Login Link"

        context = {
            "recipient_name": recipient_name,
            "verify_url": verify_url,
            "expires_minutes": LOGIN_LINK_MAX_AGE // 60,
            "open_schedule": open_schedule,
        }

        text_body = (
            f"Hello {recipient_name},\n\n"
            f"Click the link below to access the Faculty Evaluation System:\n\n"
            f"{verify_url}\n\n"
            f"This link will expire in {LOGIN_LINK_MAX_AGE // 60} minutes.\n"
            f"If you did not request this, please ignore this email."
        )

        html_body = render_to_string("evaluator/email_login_link.html", context)

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                to=[recipient_email],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()

            request.session["login_modal"] = {
                "type": "success",
                "message": f"A secure login link has been sent to {recipient_email}."
            }
        except Exception:
            request.session["login_modal"] = {
                "type": "danger",
                "message": "The login link could not be sent. Please check your email settings."
            }

        return redirect("eval_login")

    login_modal = request.session.pop("login_modal", None)

    if portal_closed and not login_modal:
        login_modal = {
            "type": "warning",
            "message": "The evaluation portal is currently closed. Please wait for the announcement of the next evaluation schedule."
        }

    context = {
        "portal_closed": portal_closed,
        "open_schedule": open_schedule,
        "login_modal": login_modal,
    }
    return render(request, "evaluator/eval_login.html", context)


def verify_login_link(request, token):
    open_schedule = get_open_schedule()

    if not open_schedule:
        messages.error(request, "The evaluation portal is currently closed.")
        return redirect("eval_login")

    signer = TimestampSigner(salt=LINK_SALT)

    try:
        unsigned_value = signer.unsign(token, max_age=LOGIN_LINK_MAX_AGE)
    except SignatureExpired:
        messages.error(request, "This login link has expired. Please request a new one.")
        return redirect("eval_login")
    except BadSignature:
        messages.error(request, "This login link is invalid.")
        return redirect("eval_login")

    try:
        user_type, raw_id = unsigned_value.split(":", 1)
        record_id = int(raw_id)
    except (ValueError, AttributeError):
        messages.error(request, "This login link is invalid.")
        return redirect("eval_login")

    request.session.flush()

    if user_type == "officer":
        officer = (
            EvaluationOfficer.objects
            .filter(id=record_id, schedule=open_schedule)
            .first()
        )

        if not officer:
            messages.error(request, "Officer account not found.")
            return redirect("eval_login")

        request.session["officer_id"] = officer.id
        request.session["officer_name"] = officer.name
        request.session["officer_email"] = officer.email
        request.session["officer_role"] = officer.role
        request.session["is_officer_authenticated"] = True

        messages.success(request, f"Welcome, {officer.name}.")
        return redirect("eval_forms")

    if user_type == "head":
        logged_in_head = (
            DepartmentHead.objects
            .select_related("department")
            .filter(id=record_id, schedule=open_schedule)
            .first()
        )

        if not logged_in_head:
            messages.error(request, "Head account not found.")
            return redirect("eval_login")

        request.session["head_id"] = logged_in_head.id
        request.session["head_name"] = logged_in_head.name
        request.session["head_email"] = logged_in_head.email
        request.session["department_id"] = logged_in_head.department.id
        request.session["department_name"] = logged_in_head.department.name
        request.session["is_head_authenticated"] = True

        messages.success(request, f"Welcome, {logged_in_head.name}.")
        return redirect("eval_forms")

    messages.error(request, "This login link is invalid.")
    return redirect("eval_login")



def _build_saved_state_for_officer(schedule, officer):
    saved_items = []

    if officer.role == "OCD":
        evaluations = (
            OfficeEvaluation.objects
            .filter(schedule=schedule, evaluator_officer=officer)
            .select_related("evaluatee_officer")
            .prefetch_related("responses")
        )

        for evaluation in evaluations:
            response_map = {}

            for response in evaluation.responses.all().order_by("section_name", "question_number"):
                section_key = (response.section_code or response.section_name or "").strip()
                if not section_key:
                    continue

                response_map.setdefault(section_key, {})[
                    str(response.question_number - 1)
                ] = response.rating

            saved_items.append({
                "evaluatee_id": str(evaluation.evaluatee_officer_id),
                "comments": evaluation.comments or "",
                "status": evaluation.status or "submitted",
                "answers": response_map,
            })

    elif officer.role == "ADAA":
        evaluations = (
            HeadEvaluation.objects
            .filter(schedule=schedule, evaluator_officer=officer)
            .select_related("evaluatee_head")
            .prefetch_related("responses")
        )

        for evaluation in evaluations:
            response_map = {}

            for response in evaluation.responses.all().order_by("section_name", "question_number"):
                section_key = (response.section_code or response.section_name or "").strip()
                if not section_key:
                    continue

                response_map.setdefault(section_key, {})[
                    str(response.question_number - 1)
                ] = response.rating

            saved_items.append({
                "evaluatee_id": str(evaluation.evaluatee_head_id),
                "comments": evaluation.comments or "",
                "status": evaluation.status or "submitted",
                "answers": response_map,
            })

    return {
        "faculty": saved_items
    }


def _build_officer_dashboard_summary(schedule, officer, officer_evaluatees):
    if officer.role == "OCD":
        saved_count = OfficeEvaluation.objects.filter(
            schedule=schedule,
            evaluator_officer=officer,
            status="submitted"
        ).count()
    elif officer.role == "ADAA":
        saved_count = HeadEvaluation.objects.filter(
            schedule=schedule,
            evaluator_officer=officer,
            status="submitted"
        ).count()
    else:
        saved_count = 0

    required_count = (
        officer_evaluatees.count()
        if hasattr(officer_evaluatees, "count")
        else len(officer_evaluatees)
    )

    return {
        "faculty": {
            "saved": saved_count,
            "required": required_count,
            "status": (
                "submitted"
                if required_count and saved_count == required_count
                else ("in_progress" if saved_count > 0 else "not_started")
            ),
        },
        "overall": {
            "saved": saved_count,
            "required": required_count,
        },
    }
    
    
    
def eval_forms(request):
    open_schedule = get_open_schedule()

    if not open_schedule:
        messages.error(request, "The evaluation portal is currently closed.")
        return redirect("eval_login")

    # OFFICER FLOW
    is_officer_authenticated = request.session.get("is_officer_authenticated")
    officer_id = request.session.get("officer_id")

    if is_officer_authenticated and officer_id:
        officer = (
            EvaluationOfficer.objects
            .filter(id=officer_id, schedule=open_schedule)
            .first()
        )

        if not officer:
            request.session.flush()
            messages.error(request, "Your session is invalid. Please log in again.")
            return redirect("eval_login")

        if officer.role == "OCD":
            officer_evaluatees = (
                EvaluationOfficer.objects
                .filter(schedule=open_schedule, role="ADAA")
                .order_by("name")
            )
            officer_department_label = "Office of the Campus Director"
        elif officer.role == "ADAA":
            officer_evaluatees = (
                DepartmentHead.objects
                .select_related("department")
                .filter(schedule=open_schedule)
                .order_by("department__name", "name")
            )
            officer_department_label = "Office of the ADAA"
        else:
            officer_evaluatees = EvaluationOfficer.objects.none()
            officer_department_label = "Office"

        saved_state = _build_saved_state_for_officer(open_schedule, officer)
        dashboard_summary = _build_officer_dashboard_summary(
            open_schedule, officer, officer_evaluatees
        )

        context = {
            "logged_in_officer": officer,
            "open_schedule": open_schedule,
            "officer_evaluatees": officer_evaluatees,
            "officer_department_label": officer_department_label,
            "saved_evaluations_json": saved_state,
            "dashboard_summary_json": dashboard_summary,
            "is_schedule_open": True,
        }
        return render(request, "evaluator/eval_forms.html", context)

    # HEAD FLOW
    is_head_authenticated = request.session.get("is_head_authenticated")
    head_id = request.session.get("head_id")
    department_id = request.session.get("department_id")

    if not is_head_authenticated or not head_id or not department_id:
        messages.error(request, "Please log in first.")
        return redirect("eval_login")

    logged_in_head = (
        DepartmentHead.objects
        .select_related("department")
        .filter(id=head_id, department_id=department_id, schedule=open_schedule)
        .first()
    )

    if not logged_in_head:
        request.session.flush()
        messages.error(request, "Your session is invalid. Please log in again.")
        return redirect("eval_login")

    department_faculty_members = (
        FacultyMember.objects
        .filter(schedule=open_schedule, department_id=logged_in_head.department_id)
        .order_by("name")
    )
    
    saved_state = _build_saved_state_for_head(open_schedule, logged_in_head)
    dashboard_summary = _build_dashboard_summary(
        open_schedule, logged_in_head, department_faculty_members
    )

    context = {
        "logged_in_head": logged_in_head,
        "open_schedule": open_schedule,
        "department_faculty_members": department_faculty_members,
        "saved_evaluations_json": saved_state,
        "dashboard_summary_json": dashboard_summary,
        "is_schedule_open": True,
    }

    return render(request, "evaluator/eval_forms.html", context)

@require_POST
def save_evaluation(request):
    open_schedule = get_open_schedule()
    if not open_schedule:
        return JsonResponse({
            "success": False,
            "message": "The evaluation portal is currently closed."
        }, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({
            "success": False,
            "message": "Invalid request payload."
        }, status=400)

    evaluatee_id = payload.get("evaluatee_id")
    comments = (payload.get("comments") or "").strip()
    answers = payload.get("answers") or {}

    if not evaluatee_id:
        return JsonResponse({
            "success": False,
            "message": "No evaluatee selected."
        }, status=400)

    cleaned_answers = []

    for section_name, section_answers in answers.items():
        if not isinstance(section_answers, dict):
            continue

        for question_number, rating in section_answers.items():
            try:
                q_no = int(question_number)
                score = int(rating)
            except (TypeError, ValueError):
                return JsonResponse({
                    "success": False,
                    "message": "Invalid rating data."
                }, status=400)

            if score not in [1, 2, 3, 4, 5]:
                return JsonResponse({
                    "success": False,
                    "message": "Ratings must be from 1 to 5 only."
                }, status=400)

            cleaned_answers.append({
                "section_code": str(section_name).strip(),
                "section_name": str(section_name).strip(),
                "question_number": q_no + 1,
                "question_text": "",
                "rating": score,
            })

    if not cleaned_answers:
        return JsonResponse({
            "success": False,
            "message": "No answers were found to save."
        }, status=400)

    total_score = sum(item["rating"] for item in cleaned_answers)
    average_score = round(total_score / len(cleaned_answers), 2) if cleaned_answers else 0

    # -------------------------------------------------
    # HEAD -> FACULTY
    # -------------------------------------------------
    if request.session.get("is_head_authenticated") and request.session.get("head_id"):
        logged_in_head = (
            DepartmentHead.objects
            .select_related("department")
            .filter(id=request.session.get("head_id"), schedule=open_schedule)
            .first()
        )

        if not logged_in_head:
            return JsonResponse({
                "success": False,
                "message": "Head session not found."
            }, status=403)

        evaluatee_faculty = (
            FacultyMember.objects
            .select_related("department")
            .filter(
                id=evaluatee_id,
                schedule=open_schedule,
                department=logged_in_head.department
            )
            .first()
        )

        if not evaluatee_faculty:
            return JsonResponse({
                "success": False,
                "message": "Selected faculty member was not found."
            }, status=404)

        with transaction.atomic():
            evaluation, _ = FacultyEvaluation.objects.update_or_create(
                schedule=open_schedule,
                evaluator_head=logged_in_head,
                evaluatee_faculty=evaluatee_faculty,
                defaults={
                    "evaluator_name": logged_in_head.name,
                    "evaluator_department": logged_in_head.department.name,
                    "evaluatee_name": evaluatee_faculty.name,
                    "evaluatee_department": evaluatee_faculty.department.name,
                    "comments": comments,
                    "status": "submitted",
                    "total_score": total_score,
                    "average_score": average_score,
                    "submitted_at": timezone.now(),
                }
            )

            FacultyEvaluationResponse.objects.filter(evaluation=evaluation).delete()
            FacultyEvaluationResponse.objects.bulk_create([
                FacultyEvaluationResponse(
                    evaluation=evaluation,
                    section_code=item["section_code"],
                    section_name=item["section_name"],
                    question_number=item["question_number"],
                    question_text=item["question_text"],
                    rating=item["rating"],
                    evaluator_name=evaluation.evaluator_name,
                    evaluator_department=evaluation.evaluator_department,
                    evaluatee_name=evaluation.evaluatee_name,
                    evaluatee_department=evaluation.evaluatee_department,
                )
                for item in cleaned_answers
            ])

        return JsonResponse({
            "success": True,
            "message": "Faculty evaluation saved successfully.",
            "evaluation_id": evaluation.id,
            "evaluatee_name": evaluation.evaluatee_name,
            "total_score": total_score,
            "average_score": average_score,
        })

    # -------------------------------------------------
    # OFFICER FLOW
    # -------------------------------------------------
    if request.session.get("is_officer_authenticated") and request.session.get("officer_id"):
        logged_in_officer = (
            EvaluationOfficer.objects
            .filter(id=request.session.get("officer_id"), schedule=open_schedule)
            .first()
        )

        if not logged_in_officer:
            return JsonResponse({
                "success": False,
                "message": "Officer session not found."
            }, status=403)

        # OCD -> ADAA
        if logged_in_officer.role == "OCD":
            evaluatee_officer = (
                EvaluationOfficer.objects
                .filter(
                    id=evaluatee_id,
                    schedule=open_schedule,
                    role="ADAA"
                )
                .first()
            )

            if not evaluatee_officer:
                return JsonResponse({
                    "success": False,
                    "message": "Selected ADAA was not found."
                }, status=404)

            with transaction.atomic():
                evaluation, _ = OfficeEvaluation.objects.update_or_create(
                    schedule=open_schedule,
                    evaluator_officer=logged_in_officer,
                    evaluatee_officer=evaluatee_officer,
                    defaults={
                        "evaluator_name": logged_in_officer.name,
                        "evaluator_role": logged_in_officer.role,
                        "evaluatee_name": evaluatee_officer.name,
                        "evaluatee_role": evaluatee_officer.role,
                        "comments": comments,
                        "status": "submitted",
                        "total_score": total_score,
                        "average_score": average_score,
                        "submitted_at": timezone.now(),
                    }
                )

                OfficeEvaluationResponse.objects.filter(evaluation=evaluation).delete()
                OfficeEvaluationResponse.objects.bulk_create([
                    OfficeEvaluationResponse(
                        evaluation=evaluation,
                        section_code=item["section_code"],
                        section_name=item["section_name"],
                        question_number=item["question_number"],
                        question_text=item["question_text"],
                        rating=item["rating"],
                        evaluator_name=evaluation.evaluator_name,
                        evaluator_role=evaluation.evaluator_role,
                        evaluatee_name=evaluation.evaluatee_name,
                        evaluatee_role=evaluation.evaluatee_role,
                    )
                    for item in cleaned_answers
                ])

            return JsonResponse({
                "success": True,
                "message": "ADAA evaluation saved successfully.",
                "evaluation_id": evaluation.id,
                "evaluatee_name": evaluation.evaluatee_name,
                "total_score": total_score,
                "average_score": average_score,
            })

        # ADAA -> HEAD
        if logged_in_officer.role == "ADAA":
            evaluatee_head = (
                DepartmentHead.objects
                .select_related("department")
                .filter(id=evaluatee_id, schedule=open_schedule)
                .first()
            )

            if not evaluatee_head:
                return JsonResponse({
                    "success": False,
                    "message": "Selected department head was not found."
                }, status=404)

            with transaction.atomic():
                evaluation, _ = HeadEvaluation.objects.update_or_create(
                    schedule=open_schedule,
                    evaluator_officer=logged_in_officer,
                    evaluatee_head=evaluatee_head,
                    defaults={
                        "evaluator_name": logged_in_officer.name,
                        "evaluator_role": logged_in_officer.role,
                        "evaluatee_name": evaluatee_head.name,
                        "evaluatee_department": evaluatee_head.department.name,
                        "comments": comments,
                        "status": "submitted",
                        "total_score": total_score,
                        "average_score": average_score,
                        "submitted_at": timezone.now(),
                    }
                )

                HeadEvaluationResponse.objects.filter(evaluation=evaluation).delete()
                HeadEvaluationResponse.objects.bulk_create([
                    HeadEvaluationResponse(
                        evaluation=evaluation,
                        section_code=item["section_code"],
                        section_name=item["section_name"],
                        question_number=item["question_number"],
                        question_text=item["question_text"],
                        rating=item["rating"],
                        evaluator_name=evaluation.evaluator_name,
                        evaluator_role=evaluation.evaluator_role,
                        evaluatee_name=evaluation.evaluatee_name,
                        evaluatee_department=evaluation.evaluatee_department,
                    )
                    for item in cleaned_answers
                ])

            return JsonResponse({
                "success": True,
                "message": "Department head evaluation saved successfully.",
                "evaluation_id": evaluation.id,
                "evaluatee_name": evaluation.evaluatee_name,
                "total_score": total_score,
                "average_score": average_score,
            })

    return JsonResponse({
        "success": False,
        "message": "You are not authorized to save this evaluation."
    }, status=403)
    

def eval_logout(request):
    keys_to_remove = [
        "head_id",
        "head_name",
        "head_email",
        "department_id",
        "department_name",
        "is_head_authenticated",

        "officer_id",
        "officer_name",
        "officer_email",
        "officer_role",
        "is_officer_authenticated",
    ]

    for key in keys_to_remove:
        request.session.pop(key, None)

    messages.success(request, "You have been logged out.")
    return redirect("eval_login")


def verify_head_login_link(request, token):
    open_schedule = get_open_schedule()

    if not open_schedule:
        messages.error(request, "The department head portal is currently closed.")
        return redirect("admin_login")

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
        .filter(id=head_id, schedule=open_schedule)
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