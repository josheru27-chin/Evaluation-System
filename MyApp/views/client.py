from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMultiAlternatives
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..models import EvaluationSchedule, DepartmentHead, FacultyMember


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


def eval_login(request):
    open_schedule = get_open_schedule()
    portal_closed = open_schedule is None

    if request.method == "POST":
        action = (request.POST.get("action") or "send_link").strip()
        email = (request.POST.get("email") or "").strip().lower()

        if portal_closed:
            messages.error(request, "The evaluation portal is currently closed.")
            return redirect("eval_login")

        if action != "send_link":
            action = "send_link"

        if not email:
            messages.error(request, "Please enter your email address.")
            return redirect("eval_login")

        head = (
            DepartmentHead.objects
            .select_related("department")
            .filter(email__iexact=email)
            .first()
        )

        faculty = (
            FacultyMember.objects
            .select_related("department")
            .filter(email__iexact=email)
            .first()
        )

        if not head:
            if faculty:
                messages.error(
                    request,
                    "This account is registered as faculty only. Faculty members are not allowed to access the head evaluation portal."
                )
            else:
                messages.error(
                    request,
                    "This email is not registered in the evaluation system."
                )
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

            messages.success(
                request,
                f"A secure login link has been sent to {head.email}."
            )
        except Exception:
            messages.error(
                request,
                "The login link could not be sent. Please check your email settings."
            )

        return redirect("eval_login")

    context = {
        "portal_closed": portal_closed,
        "open_schedule": open_schedule,
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
        .filter(id=head_id)
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
        .filter(id=head_id, department_id=department_id)
        .first()
    )

    if not logged_in_head:
        request.session.flush()
        messages.error(request, "Your session is invalid. Please log in again.")
        return redirect("eval_login")

    department_faculty_members = (
        FacultyMember.objects
        .filter(department_id=logged_in_head.department_id)
        .order_by("name")
    )

    other_heads = (
        DepartmentHead.objects
        .select_related("department")
        .exclude(id=logged_in_head.id)
        .order_by("name")
    )

    context = {
        "logged_in_head": logged_in_head,
        "open_schedule": open_schedule,
        "department_faculty_members": department_faculty_members,
        "other_heads": other_heads,
    }

    return render(request, "evaluator/eval_forms.html", context)


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