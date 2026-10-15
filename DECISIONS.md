# Technical Decisions — Employee Attendance & Analytics API

## 1. FastAPI for the API layer

**Decision:** Use FastAPI to implement the REST API.

**Rationale:** FastAPI supports typed request parameters, request validation, HTTP error responses, and automatically generated interactive API documentation.

**Implementation:** The application exposes endpoints for employee management, attendance tracking, and analytics.

## 2. MongoDB with PyMongo

**Decision:** Use MongoDB as the database and PyMongo for database operations.

**Rationale:** MongoDB stores employee and attendance records as documents and supports aggregation pipelines for reporting.

**Implementation:** The application uses the `employees` and `attendance_logs` collections.

## 3. Pydantic request models

**Decision:** Validate incoming request bodies using Pydantic models.

**Rationale:** Explicit validation helps reject malformed input before business logic executes.

**Implementation:** Models define employee fields, attendance timestamps, permitted statuses, and regularization inputs.

## 4. Employee codes as business identifiers

**Decision:** Use `emp_code` to identify employees in application operations.

**Rationale:** A stable employee code is easier to use in attendance requests and reporting than exposing database-specific identifiers.

**Implementation:** Employee codes follow the `EMP` prefix and a four-to-six-digit suffix. A unique database index enforces uniqueness.

## 5. String-based attendance dates

**Decision:** Store attendance dates in `YYYY-MM-DD` format.

**Rationale:** Consistently formatted ISO dates support straightforward calendar-date filtering and grouping.

**Implementation:** Attendance records use the `date` field, while punch timestamps are stored as datetimes.

## 6. UTC timestamps and Indian Standard Time calculations

**Decision:** Normalize stored instants to UTC and use Indian Standard Time for local attendance calculations.

**Rationale:** Separating stored instants from local shift calculations helps maintain consistent timestamps while applying local working hours.

**Implementation:** Helper functions convert timestamps, determine attendance dates, and calculate shift-related values.

## 7. Centralized attendance calculations

**Decision:** Implement reusable functions for late minutes, work hours, overtime, and attendance-date determination.

**Rationale:** Shared functions reduce duplicated business logic across attendance operations.

**Implementation:** The functions are defined near the beginning of `app/main.py` and used by the relevant endpoints.

## 8. Database indexes

**Decision:** Create indexes for employee codes, department and joining-date queries, and attendance lookups.

**Rationale:** These indexes support common filtering, reporting, and duplicate-prevention operations.

**Implementation:** Indexes are created during application startup. Unique indexes enforce employee-code and employee-date attendance uniqueness.

## 9. Attendance regularization with history

**Decision:** Record changes made through attendance regularization, including the reason and regularizing person.

**Rationale:** An audit trail helps explain changes to attendance data.

**Implementation:** The regularization endpoint appends change details to the attendance record and uses an update filter to detect concurrent modifications.

## 10. MongoDB aggregation for analytics

**Decision:** Use MongoDB aggregation pipelines for reporting endpoints where database-side grouping and calculations are useful.

**Rationale:** Aggregation pipelines support filtering, grouping, joining collections, ranking, and window calculations.

**Implementation:** The application includes monthly employee analytics, department summaries, a late-arrival leaderboard, and department trends.

## 11. Department trend calculation

**Decision:** Calculate daily headcount independently of attendance-record availability.

**Rationale:** An employee who has joined the department should contribute to headcount even if no attendance record exists for a particular day.

**Implementation:** The department trend endpoint retrieves department employees and attendance records separately, calculates daily counts, and computes a seven-calendar-day moving average from available attendance rates.

## 12. Configuration through environment variables

**Decision:** Read database configuration from `MONGO_URI` and `MONGO_DB`.

**Rationale:** Configuration should be supplied by the deployment environment rather than hard-coded into the application.

**Implementation:** The application loads environment configuration and uses local defaults when variables are not provided. Deployment credentials must not be committed to the repository.

## 13. API documentation

**Decision:** Use FastAPI's generated OpenAPI documentation.

**Rationale:** Interactive documentation makes it easier to inspect endpoints and test request and response behavior.

**Implementation:** When the application is running locally, the documentation is available at `/docs`.

## Scope

These decisions describe the implementation visible in the application source. They do not imply that authentication, deployment hardening, comprehensive automated tests, or production monitoring have already been implemented.