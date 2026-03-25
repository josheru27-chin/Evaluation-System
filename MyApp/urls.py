from django.urls import path
from . import views

urlpatterns = [
    # Evaluator
    path('', views.eval_login, name='eval_login'),
    path('form/', views.eval_forms, name='eval_forms'),

    # Admin
    path('dashboard/login/', views.admin_login, name='admin_login'),
    path('dashboard/department/', views.admin_department, name='admin_department'),
    path('dashboard/results-summary/', views.admin_results_summary, name='admin_results_summary'),
    path('dashboard/manage/', views.admin_manage, name='admin_manage'),

    # Department actions
    path('dashboard/department/add/', views.add_department, name='add_department'),
    path('dashboard/department/update/<int:dept_id>/', views.update_department, name='update_department'),
    path('dashboard/department/delete/<int:dept_id>/', views.delete_department, name='delete_department'),
]