from django.shortcuts import render


# Evaluator-----------------------------------------------------------------------------------------

def eval_login(request):
    return render(request, 'evaluator/eval_login.html')

def eval_forms(request):
    return render(request, 'evaluator/eval_forms.html')


# Admin---------------------------------------------------------------------------------------------

def admin_login(request):
    return render(request, 'admin/admin_login.html')

def admin_department(request):
    return render(request, 'admin/admin_department.html', {
        'active_page': 'department',
    })

def admin_results_summary(request):
    return render(request, 'admin/admin_overall.html', {
        'active_page': 'results_summary',
    })

def admin_manage(request):
    return render(request, 'admin/admin_manage.html', {
        'active_page': 'manage',
    })




# Head----------------------------------------------------------------------------------------------

def head_add(request):
    return render(request, 'head/head_add.html')

def head_monitor(request):
    return render(request, 'head/head_monitor.html')