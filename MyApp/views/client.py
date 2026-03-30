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
    FacultyMember,
    HeadEvaluation,
    HeadEvaluationResponse,
    FacultyEvaluation,
    FacultyEvaluationResponse,
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
    head_saved = []
    faculty_saved = []

    head_evaluations = (
        HeadEvaluation.objects
        .filter(schedule=schedule, evaluator_head=logged_in_head)
        .select_related("evaluatee_head")
        .prefetch_related("responses")
    )

    for evaluation in head_evaluations:
        response_map = {}
        for response in evaluation.responses.all().order_by("section_name", "question_number"):
            section_key = (response.section_name or "").strip()
            if not section_key:
                continue
            response_map.setdefault(section_key, {})[str(response.question_number - 1)] = response.rating

        head_saved.append({
            "evaluatee_id": str(evaluation.evaluatee_head_id),
            "comments": evaluation.comments or "",
            "status": evaluation.status or "submitted",
            "answers": response_map,
        })

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
        "head_peer": head_saved,
        "faculty": faculty_saved,
    }


def _build_dashboard_summary(schedule, logged_in_head, other_heads, department_faculty_members):
    head_saved_count = HeadEvaluation.objects.filter(
        schedule=schedule, evaluator_head=logged_in_head, status="submitted"
    ).count()
    faculty_saved_count = FacultyEvaluation.objects.filter(
        schedule=schedule, evaluator_head=logged_in_head, status="submitted"
    ).count()

    head_required_count = other_heads.count() if hasattr(other_heads, "count") else len(other_heads)
    faculty_required_count = department_faculty_members.count() if hasattr(department_faculty_members, "count") else len(department_faculty_members)

    return {
        "head_peer": {
            "saved": head_saved_count,
            "required": head_required_count,
            "status": "submitted" if head_required_count and head_saved_count == head_required_count else ("in_progress" if head_saved_count > 0 else "not_started"),
        },
        "faculty": {
            "saved": faculty_saved_count,
            "required": faculty_required_count,
            "status": "submitted" if faculty_required_count and faculty_saved_count == faculty_required_count else ("in_progress" if faculty_saved_count > 0 else "not_started"),
        },
        "overall": {
            "saved": head_saved_count + faculty_saved_count,
            "required": head_required_count + faculty_required_count,
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

        if not head:
            if faculty:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "This account is registered as faculty only. Faculty members are not allowed to access the head evaluation portal."
                }
            else:
                request.session["login_modal"] = {
                    "type": "danger",
                    "message": "This email is not registered in the evaluation system."
                }
            return redirect("eval_login")

        signer = TimestampSigner(salt=LINK_SALT)
        token = signer.sign(str(head.id))

        verify_url = request.build_absolute_uri(
            reverse("verify_login_link", args=[token])
        )

        subject = "Faculty Evaluation Login Link"

        context = {
            "head": head,
            "verify_url": verify_url,
            "expires_minutes": LOGIN_LINK_MAX_AGE // 60,
            "open_schedule": open_schedule,
        }

        text_body = (
            f"Hello {head.name},\n\n"
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
        head_id = int(unsigned_value)
    except SignatureExpired:
        messages.error(request, "This login link has expired. Please request a new one.")
        return redirect("eval_login")
    except (BadSignature, ValueError):
        messages.error(request, "This login link is invalid.")
        return redirect("eval_login")

    logged_in_head = (
        DepartmentHead.objects
        .select_related("department")
        .filter(id=head_id, schedule=open_schedule)
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


def eval_forms(request):
    open_schedule = get_open_schedule()

    if not open_schedule:
        messages.error(request, "The evaluation portal is currently closed.")
        return redirect("eval_login")

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

    other_heads = (
        DepartmentHead.objects
        .filter(schedule=open_schedule)
        .exclude(id=logged_in_head.id)
        .order_by("name")
    )

    saved_state = _build_saved_state_for_head(open_schedule, logged_in_head)
    dashboard_summary = _build_dashboard_summary(
        open_schedule, logged_in_head, other_heads, department_faculty_members
    )

    context = {
        "logged_in_head": logged_in_head,
        "open_schedule": open_schedule,
        "department_faculty_members": department_faculty_members,
        "other_heads": other_heads,
        "saved_evaluations_json": saved_state,
        "dashboard_summary_json": dashboard_summary,
    }

    return render(request, "evaluator/eval_forms.html", context)


@require_POST
def save_evaluation(request):
    open_schedule = get_open_schedule()

    if not open_schedule:
        return JsonResponse({
            "success": False,
            "message": "The evaluation portal is currently closed."
        }, status=403)

    is_head_authenticated = request.session.get("is_head_authenticated")
    head_id = request.session.get("head_id")
    department_id = request.session.get("department_id")

    if not is_head_authenticated or not head_id or not department_id:
        return JsonResponse({
            "success": False,
            "message": "Please log in first."
        }, status=401)

    logged_in_head = (
        DepartmentHead.objects
        .select_related("department")
        .filter(id=head_id, department_id=department_id, schedule=open_schedule)
        .first()
    )

    if not logged_in_head:
        request.session.flush()
        return JsonResponse({
            "success": False,
            "message": "Your session is invalid. Please log in again."
        }, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({
            "success": False,
            "message": "Invalid request data."
        }, status=400)

    category = (payload.get("category") or "").strip()
    evaluatee_id = str(payload.get("evaluatee_id") or "").strip()
    comments = (payload.get("comments") or "").strip()
    answers = payload.get("answers") or {}

    if category not in ["head_peer", "faculty"]:
        return JsonResponse({
            "success": False,
            "message": "Invalid category."
        }, status=400)

    if not evaluatee_id:
        return JsonResponse({
            "success": False,
            "message": "Please select a person to evaluate."
        }, status=400)

    evaluatee_head = None
    evaluatee_faculty = None
    db_category = "head" if category == "head_peer" else "faculty"

    if category == "head_peer":
        evaluatee_head = (
            DepartmentHead.objects
            .select_related("department")
            .filter(id=evaluatee_id, schedule=open_schedule)
            .exclude(id=logged_in_head.id)
            .first()
        )

        if not evaluatee_head:
            return JsonResponse({
                "success": False,
                "message": "Selected head was not found."
            }, status=404)

    elif category == "faculty":
        evaluatee_faculty = (
            FacultyMember.objects
            .select_related("department")
            .filter(
                id=evaluatee_id,
                schedule=open_schedule,
                department_id=logged_in_head.department_id
            )
            .first()
        )

        if not evaluatee_faculty:
            return JsonResponse({
                "success": False,
                "message": "Selected faculty was not found."
            }, status=404)

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

    if db_category == "head":
        evaluatee_name = evaluatee_head.name
        evaluatee_department = evaluatee_head.department.name
    else:
        evaluatee_name = evaluatee_faculty.name
        evaluatee_department = evaluatee_faculty.department.name

    total_score = sum(item["rating"] for item in cleaned_answers)
    average_score = round(total_score / len(cleaned_answers), 2) if cleaned_answers else 0

    with transaction.atomic():
        if db_category == "head":
            evaluation, created = HeadEvaluation.objects.update_or_create(
                schedule=open_schedule,
                evaluator_head=logged_in_head,
                evaluatee_head=evaluatee_head,
                defaults={
                    "evaluator_name": logged_in_head.name,
                    "evaluator_department": logged_in_head.department.name,
                    "evaluatee_name": evaluatee_name,
                    "evaluatee_department": evaluatee_department,
                    "comments": comments,
                    "status": "submitted",
                    "total_score": total_score,
                    "average_score": average_score,
                    "submitted_at": timezone.now(),
                    "updated_at": timezone.now(),
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
                    evaluator_department=evaluation.evaluator_department,
                    evaluatee_name=evaluation.evaluatee_name,
                    evaluatee_department=evaluation.evaluatee_department,
                )
                for item in cleaned_answers
            ])
        else:
            evaluation, created = FacultyEvaluation.objects.update_or_create(
                schedule=open_schedule,
                evaluator_head=logged_in_head,
                evaluatee_faculty=evaluatee_faculty,
                defaults={
                    "evaluator_name": logged_in_head.name,
                    "evaluator_department": logged_in_head.department.name,
                    "evaluatee_name": evaluatee_name,
                    "evaluatee_department": evaluatee_department,
                    "comments": comments,
                    "status": "submitted",
                    "total_score": total_score,
                    "average_score": average_score,
                    "submitted_at": timezone.now(),
                    "updated_at": timezone.now(),
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
        "message": "Evaluation saved successfully.",
        "evaluation_id": evaluation.id,
        "evaluatee_name": evaluation.evaluatee_name,
        "total_score": total_score,
        "average_score": average_score,
    })


def eval_logout(request):
    keys_to_remove = [
        "head_id",
        "head_name",
        "head_email",
        "department_id",
        "department_name",
        "is_head_authenticated",
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