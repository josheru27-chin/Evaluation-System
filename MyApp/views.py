from django.shortcuts import render

def eval_login(request):
    return render(request, 'eval_login.html')

def eval_forms(request):
    return render(request, 'eval_forms.html')