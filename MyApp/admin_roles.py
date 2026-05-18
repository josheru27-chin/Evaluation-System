ROLE_UITC = "UITC"
ROLE_ADAA = "ADAA"
ROLE_STAFF = "STAFF"


def get_admin_role(user):
    if not user or not user.is_authenticated:
        return ""

    if user.is_superuser:
        return ROLE_UITC

    group_names = set(user.groups.values_list("name", flat=True))

    if ROLE_ADAA in group_names:
        return ROLE_ADAA

    if ROLE_STAFF in group_names:
        return ROLE_STAFF

    return ""


def admin_landing_page(user):
    role = get_admin_role(user)

    if role in [ROLE_UITC, ROLE_ADAA]:
        return "admin_manage"

    if role == ROLE_STAFF:
        return "admin_results_summary"

    return "admin_login"


def can_access_role(user, allowed_roles):
    role = get_admin_role(user)
    return user.is_authenticated and user.is_staff and role in allowed_roles