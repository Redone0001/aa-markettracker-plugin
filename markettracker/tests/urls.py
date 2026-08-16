from django.urls import include, path

urlpatterns = [
    path("markettracker/", include("markettracker.urls")),
]
