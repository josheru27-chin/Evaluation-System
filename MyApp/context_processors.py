from .admin_roles import get_admin_role, ROLE_UITC, ROLE_ADAA, ROLE_STAFF


def admin_role_context(request):
    user = getattr(request, "user", None)
    admin_role = get_admin_role(user)

    return {
        "admin_role": admin_role,
        "is_uitc_admin": admin_role == ROLE_UITC,
        "is_adaa_admin": admin_role == ROLE_ADAA,
        "is_staff_admin": admin_role == ROLE_STAFF,

        "can_full_admin": admin_role in [ROLE_UITC, ROLE_ADAA],
        "can_view_results": admin_role in [ROLE_UITC, ROLE_ADAA, ROLE_STAFF],
        "can_manage_users": admin_role in [ROLE_UITC, ROLE_ADAA],
    }