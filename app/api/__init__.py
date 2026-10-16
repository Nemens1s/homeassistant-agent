"""HTTP routers. Each module owns one group of /api endpoints and reads its
dependencies from `request.app.state`, which the lifespan in app.main fills."""
