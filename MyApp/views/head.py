from django.shortcuts import render, redirect



def head_add(request):
    return render(request, 'head/head_add.html')

def head_monitor(request):
    return render(request, 'head/head_monitor.html')