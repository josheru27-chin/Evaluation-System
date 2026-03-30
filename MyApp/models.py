from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator


class Department(models.Model):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


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


class FacultyMember(models.Model):
    schedule = models.ForeignKey(
        "EvaluationSchedule",
        on_delete=models.CASCADE,
        related_name="faculty_members",
        null=True,
        blank=True
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="faculty_members"
    )
    id_number = models.CharField(max_length=100, blank=True, default="")
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DepartmentHead(models.Model):
    schedule = models.ForeignKey(
        "EvaluationSchedule",
        on_delete=models.CASCADE,
        related_name="department_heads",
        null=True,
        blank=True
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="heads"
    )
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")
    otp_code = models.CharField(max_length=6, blank=True, default="")
    otp_created_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)

    class Meta:
        ordering = ["department__name", "name"]

    def __str__(self):
        return f"{self.name} - {self.department.name}"


# =========================
# FOR EVALUATION RESULTS
# =========================

class HeadEvaluation(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
    ]

    schedule = models.ForeignKey(
        EvaluationSchedule,
        on_delete=models.CASCADE,
        related_name="head_evaluations"
    )

    evaluator_head = models.ForeignKey(
        DepartmentHead,
        on_delete=models.CASCADE,
        related_name="submitted_head_evaluations"
    )
    evaluator_name = models.CharField(max_length=255, blank=True, default="")
    evaluator_department = models.CharField(max_length=255, blank=True, default="")

    evaluatee_head = models.ForeignKey(
        DepartmentHead,
        on_delete=models.CASCADE,
        related_name="received_head_evaluations"
    )
    evaluatee_name = models.CharField(max_length=255, blank=True, default="")
    evaluatee_department = models.CharField(max_length=255, blank=True, default="")

    comments = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="submitted")

    total_score = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True
    )
    average_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-submitted_at"]
        unique_together = ("schedule", "evaluator_head", "evaluatee_head")
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["submitted_at"]),
            models.Index(fields=["evaluator_head"]),
            models.Index(fields=["evaluatee_head"]),
        ]

    def __str__(self):
        return f"{self.evaluator_name} evaluated head {self.evaluatee_name}"

    def save(self, *args, **kwargs):
        if self.evaluator_head:
            if not self.evaluator_name:
                self.evaluator_name = self.evaluator_head.name
            if not self.evaluator_department:
                self.evaluator_department = self.evaluator_head.department.name

        if self.evaluatee_head:
            if not self.evaluatee_name:
                self.evaluatee_name = self.evaluatee_head.name
            if not self.evaluatee_department:
                self.evaluatee_department = self.evaluatee_head.department.name

        self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class HeadEvaluationResponse(models.Model):
    evaluation = models.ForeignKey(
        HeadEvaluation,
        on_delete=models.CASCADE,
        related_name="responses"
    )

    section_code = models.CharField(max_length=100, blank=True, default="")
    section_name = models.CharField(max_length=255)
    question_number = models.PositiveIntegerField()
    question_text = models.TextField(blank=True, default="")

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )

    evaluator_name = models.CharField(max_length=255, blank=True, default="")
    evaluator_department = models.CharField(max_length=255, blank=True, default="")
    evaluatee_name = models.CharField(max_length=255, blank=True, default="")
    evaluatee_department = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["section_name", "question_number"]
        unique_together = ("evaluation", "section_name", "question_number")
        indexes = [
            models.Index(fields=["section_code"]),
            models.Index(fields=["rating"]),
            models.Index(fields=["evaluatee_name"]),
            models.Index(fields=["evaluatee_department"]),
            models.Index(fields=["evaluator_name"]),
        ]

    def __str__(self):
        return f"{self.evaluatee_name} - {self.section_name} Q{self.question_number}: {self.rating}"

    def save(self, *args, **kwargs):
        if self.evaluation:
            if not self.evaluator_name:
                self.evaluator_name = self.evaluation.evaluator_name
            if not self.evaluator_department:
                self.evaluator_department = self.evaluation.evaluator_department
            if not self.evaluatee_name:
                self.evaluatee_name = self.evaluation.evaluatee_name
            if not self.evaluatee_department:
                self.evaluatee_department = self.evaluation.evaluatee_department

        self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class FacultyEvaluation(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
    ]

    schedule = models.ForeignKey(
        EvaluationSchedule,
        on_delete=models.CASCADE,
        related_name="faculty_evaluations"
    )

    evaluator_head = models.ForeignKey(
        DepartmentHead,
        on_delete=models.CASCADE,
        related_name="submitted_faculty_evaluations"
    )
    evaluator_name = models.CharField(max_length=255, blank=True, default="")
    evaluator_department = models.CharField(max_length=255, blank=True, default="")

    evaluatee_faculty = models.ForeignKey(
        FacultyMember,
        on_delete=models.CASCADE,
        related_name="received_faculty_evaluations"
    )
    evaluatee_name = models.CharField(max_length=255, blank=True, default="")
    evaluatee_department = models.CharField(max_length=255, blank=True, default="")

    comments = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="submitted")

    total_score = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True
    )
    average_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-submitted_at"]
        unique_together = ("schedule", "evaluator_head", "evaluatee_faculty")
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["submitted_at"]),
            models.Index(fields=["evaluator_head"]),
            models.Index(fields=["evaluatee_faculty"]),
        ]

    def __str__(self):
        return f"{self.evaluator_name} evaluated faculty {self.evaluatee_name}"

    def save(self, *args, **kwargs):
        if self.evaluator_head:
            if not self.evaluator_name:
                self.evaluator_name = self.evaluator_head.name
            if not self.evaluator_department:
                self.evaluator_department = self.evaluator_head.department.name

        if self.evaluatee_faculty:
            if not self.evaluatee_name:
                self.evaluatee_name = self.evaluatee_faculty.name
            if not self.evaluatee_department:
                self.evaluatee_department = self.evaluatee_faculty.department.name

        self.updated_at = timezone.now()
        super().save(*args, **kwargs)


class FacultyEvaluationResponse(models.Model):
    evaluation = models.ForeignKey(
        FacultyEvaluation,
        on_delete=models.CASCADE,
        related_name="responses"
    )

    section_code = models.CharField(max_length=100, blank=True, default="")
    section_name = models.CharField(max_length=255)
    question_number = models.PositiveIntegerField()
    question_text = models.TextField(blank=True, default="")

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )

    evaluator_name = models.CharField(max_length=255, blank=True, default="")
    evaluator_department = models.CharField(max_length=255, blank=True, default="")
    evaluatee_name = models.CharField(max_length=255, blank=True, default="")
    evaluatee_department = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["section_name", "question_number"]
        unique_together = ("evaluation", "section_name", "question_number")

    def __str__(self):
        return f"{self.evaluatee_name} - {self.section_name} Q{self.question_number}: {self.rating}"

    def save(self, *args, **kwargs):
        if self.evaluation:
            if not self.evaluator_name:
                self.evaluator_name = self.evaluation.evaluator_name
            if not self.evaluator_department:
                self.evaluator_department = self.evaluation.evaluator_department
            if not self.evaluatee_name:
                self.evaluatee_name = self.evaluation.evaluatee_name
            if not self.evaluatee_department:
                self.evaluatee_department = self.evaluation.evaluatee_department

        self.updated_at = timezone.now()
        super().save(*args, **kwargs)


# =========================
# EVALUATION FORM STRUCTURE
# =========================

class EvaluationSection(models.Model):
    CATEGORY_CHOICES = [
        ("head", "Head Evaluation"),
        ("faculty", "Faculty Evaluation"),
    ]

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)

    class Meta:
        ordering = ["category", "code", "name"]

    def __str__(self):
        return f"{self.category} - {self.name}"


class EvaluationQuestion(models.Model):
    section = models.ForeignKey(
        EvaluationSection,
        on_delete=models.CASCADE,
        related_name="questions"
    )
    question_number = models.PositiveIntegerField()
    text = models.TextField()

    class Meta:
        ordering = ["section", "question_number"]
        unique_together = ("section", "question_number")

    def __str__(self):
        return f"{self.section.name} Q{self.question_number}"