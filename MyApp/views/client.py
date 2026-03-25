from django.shortcuts import render, redirect
from django.utils import timezone
from ..models import EvaluationSchedule


def eval_login(request):
    now = timezone.localtime(timezone.now())

    open_schedule = (
        EvaluationSchedule.objects
        .filter(start_datetime__lte=now, end_datetime__gte=now)
        .order_by("start_datetime")
        .first()
    )

    portal_closed = open_schedule is None

    context = {
        "portal_closed": portal_closed,
        "open_schedule": open_schedule,
    }

    return render(request, "evaluator/eval_login.html", context)


def eval_forms(request):
    now = timezone.localtime(timezone.now())

    open_schedule = (
        EvaluationSchedule.objects
        .filter(start_datetime__lte=now, end_datetime__gte=now)
        .first()
    )

    if not open_schedule:
        return redirect("eval_login")

    return render(request, "evaluator/eval_forms.html")