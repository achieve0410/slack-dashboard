from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.generic import TemplateView


frontend_view = login_required(
    TemplateView.as_view(template_name="frontend/index.html")
)


urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    path("api/v1/", include("dashboard.platform_urls")),
    path("api/", include("dashboard.urls")),
    re_path(
        r"^(?!api/|admin/|static/).*$",
        frontend_view,
        name="frontend",
    ),
]
