from django.urls import path
from . import views

urlpatterns = [
    # Evaluator
    path('', views.eval_login, name='eval_login'),
    path('form/', views.eval_forms, name='eval_forms'),

    # Custom Admin
    path('dashboard/login/', views.admin_login, name='admin_login'),
    path('dashboard/base/', views.admin_base, name='admin_base'),
    path('dashboard/department/', views.admin_department, name='admin_department'),
    path('dashboard/manage/', views.admin_manage, name='admin_manage'),
    path('dashboard/overall/', views.admin_overall, name='admin_overall'),
    path('dashboard/sidebar/', views.admin_sidebar, name='admin_sidebar'),

    # Head
    path('head/add/', views.head_add, name='head_add'),
    path('head/monitor/', views.head_monitor, name='head_monitor'),
]