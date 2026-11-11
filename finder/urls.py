from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.FinderLoginView.as_view(), name="login"),
    path("register/", views.register, name="register"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("api/search", views.search, name="search"),
    path("api/scene", views.scene, name="scene"),
    path("api/clips", views.clips, name="clips"),
    path("api/annotate", views.annotate_view, name="annotate"),
    path("api/annotated-image", views.annotated_image_view, name="annotated_image"),
    path("api/route", views.route_view, name="route"),
    path("api/damage", views.damage_view, name="damage"),
    # New interactive-map endpoints
    path("api/context", views.context_view, name="context"),
    path("api/routes", views.routes_view, name="routes"),
]
