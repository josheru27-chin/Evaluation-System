from django.shortcuts import render


# Evaluator-----------------------------------------------------------------------------------------

def eval_login(request):
    return render(request, 'Evaluator/eval_login.html')

def eval_forms(request):
    return render(request, 'Evaluator/eval_forms.html')


# Admin---------------------------------------------------------------------------------------------

def admin_login(request):
    return render(request, 'Admin/admin_login.html')

def admin_base(request):
    return render(request, 'Admin/admin_base.html')

def admin_department(request):
    return render(request, 'Admin/admin_department.html')

def admin_manage(request):
    return render(request, 'Admin/admin_manage.html')

def admin_overall(request):
    return render(request, 'Admin/admin_overall.html')

def admin_sidebar(request):
    return render(request, 'Admin/admin_sidebar.html')



# Head----------------------------------------------------------------------------------------------

def head_add(request):
    return render(request, 'Head/head_add.html')

def head_monitor(request):
    return render(request, 'Head/head_monitor.html')