# Tech Stack: Laravel + Vue 3 (Inertia.js SPA)

## Architecture
- **Backend**: Laravel PHP — handles auth, API routes, business logic
- **Frontend**: Vue 3 SPA via Inertia.js — NO classic Blade-rendered HTML pages

## Where things live in the workspace

### Frontend (Vue) — ALWAYS look here for UI changes
- `resources/js/Pages/` — full-page components (Login, Dashboard, etc.)
- `resources/js/Pages/Auth/` — authentication screens (Login.vue, Register.vue, ForgotPassword.vue)
- `resources/js/Components/` — reusable UI components
- `resources/js/Layouts/` — layout wrappers (AppLayout, AuthLayout, etc.)
- `resources/js/app.js` — Inertia app bootstrap

### Backend (Laravel PHP)
- `routes/web.php` — web routes (return `Inertia::render('PageName')`, NOT `view('blade')`)
- `routes/api.php` — API-only routes
- `app/Http/Controllers/` — controller classes
- `app/Models/` — Eloquent models
- `database/migrations/` — DB schema

### Blade (shell only — do NOT edit for UI)
- `resources/views/app.blade.php` — the single Inertia shell, contains `@inertia`. 
  This is NOT a page. NEVER put UI code here.
- All actual page HTML/CSS is in the Vue `Pages/` files above.

## Navigation rules for the LLM
- User says "change the login page" → edit `resources/js/Pages/Auth/Login.vue`
- User says "update the dashboard" → edit `resources/js/Pages/Dashboard.vue` (or similar)
- User says "change the navbar/header" → check `resources/js/Layouts/` and `resources/js/Components/`
- User says "add a new page" → create a Vue file in `resources/js/Pages/` and add a route in `routes/web.php` using `Inertia::render()`
- User says "change backend logic / API" → edit controllers in `app/Http/Controllers/`
- NEVER edit `*.blade.php` for page UI unless it is `app.blade.php` and the user explicitly asks to change the HTML shell

## Running the frontend build
- Dev (HMR): already running via Vite inside the instance — no need to restart
- Production build: `npm run build` inside the app container
