from django.db import models
from django.utils import timezone


class Department(models.Model):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class FacultyMember(models.Model):
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="faculty_members"
    )
    id_number = models.CharField(max_length=100, blank=True, default="")
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")

    def __str__(self):
        return self.name


class DepartmentHead(models.Model):
    department = models.OneToOneField(
        Department,
        on_delete=models.CASCADE,
        related_name="head"
    )
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")

    def __str__(self):
        return f"{self.name} - {self.department.name}"


class EvaluationSchedule(models.Model):
    title = models.CharField(max_length=255)
    academic_year = models.CharField(max_length=50)
    semester = models.CharField(max_length=50)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_datetime", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def computed_status(self):
        now = timezone.localtime(timezone.now())
        if self.start_datetime <= now <= self.end_datetime:
            return "Open"
        return "Closed"

    @property
    def is_open_now(self):
        now = timezone.localtime(timezone.now())
        return self.start_datetime <= now <= self.end_datetime