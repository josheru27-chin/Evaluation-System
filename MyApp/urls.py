from django.urls import path
from . import views

urlpatterns = [
    path('', views.eval_login, name='eval_login'),
    path('form/', views.eval_forms, name='eval_forms'),
]