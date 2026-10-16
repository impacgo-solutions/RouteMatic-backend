"""Declarative description of the tenant data model.

Derived from the Flutter domain models in ``lib/domain/models/*.dart`` and the
enums in ``lib/domain/enums/enums.dart`` (the former ``LocalDatabase`` tables).
The same spec drives DDL generation, request validation and the generic CRUD
repository, so the API JSON stays identical to the Dart ``toJson`` shapes
(camelCase keys, enum *names* as strings, ISO-8601 timestamps).

Schemas (see docs/ARCHITECTURE.md): every entity has a HOME.
  fleet    -> lives in the single ``fleet_workspace`` schema (fleet-level data)
  company  -> lives in each ``tenant_<company>`` schema (company-level data)
  both     -> a separate physical table in each (each scope sees only its own rows)
Foreign keys are resolved per schema kind: same-schema -> normal FK, company -> fleet
-> cross-schema FK, fleet -> company -> no FK (soft reference, validated by the API).

Column kinds: text, int, num, bool, ts (timestamptz), jsonb.
List kinds (children of an entity):
  Ids      - list of ids/strings stored as rows of a child table (optional FK)
  Objs     - list of small objects stored as rows of a child table
  Kv       - map<string, scalar> stored as key/value rows
  Derived  - read-only list computed from another table
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Enums (Dart enum *names*)
# --------------------------------------------------------------------------- #
ENUMS: dict[str, list[str]] = {
    "UserRole": ["employee", "driver", "employerAdmin", "fleetManager", "system"],
    "RecordStatus": ["active", "inactive"],
    "VehicleType": ["sedan", "suv", "mpv", "van", "minibus"],
    "Propulsion": ["ev", "ice", "hybrid"],
    "LuxuryClass": ["standard", "premium", "luxury"],
    "VehicleStatus": ["available", "assigned", "onTrip", "maintenance", "unavailable"],
    "DriverStatus": ["available", "assigned", "onTrip", "offDuty", "unavailable"],
    "AttendanceStatus": ["notCheckedIn", "checkedIn", "late", "checkedOut"],
    "SafetyChecklistStatus": ["pending", "passed", "failed", "needsReview"],
    "LocationType": ["pickup", "destination", "office", "parking", "depot", "intermediateStop"],
    "TripStatus": [
        "draft", "planned", "assigned", "driverAccepted", "ready", "inProgress",
        "delayed", "completed", "cancelled", "needsAttention",
    ],
    "BillableStatus": ["notYetBillable", "missingDistance", "billed", "excluded"],
    "RouteStatus": ["draft", "planned", "active", "completed", "cancelled"],
    "StopStatus": ["upcoming", "arrived", "completed", "skipped", "issue"],
    "StopKind": ["pickup", "drop"],
    "BookingStatus": [
        "requested", "approved", "assigned", "waitlisted", "rejected", "cancelled", "completed",
    ],
    "PassengerEventType": ["boarded", "alighted", "noShow", "manualOverride"],
    "VerificationMethod": ["qrDemo", "tpinDemo", "manualAuthorized"],
    "VerificationStatus": ["verified", "invalid", "expired", "duplicate", "wrongTrip", "overridden"],
    "ShiftStatus": ["scheduled", "checkedIn", "late", "regularizationPending", "completed"],
    "RegularizationStatus": ["pending", "approved", "rejected"],
    "CheckResult": ["pass", "fail", "notApplicable"],
    "InspectionOutcome": ["passed", "failed", "needsReview"],
    "IncidentType": [
        "delay", "routeDeviation", "breakdown", "vehicleDefect", "passengerIssue",
        "safety", "emergency", "other",
    ],
    "IncidentSeverity": ["low", "medium", "high", "critical"],
    "IncidentStatus": ["draft", "submittedDemo", "acknowledgedDemo", "resolvedDemo", "failedDemo"],
    "BillingStatus": ["draft", "generated", "reviewed", "paidDemo", "disputed"],
    "OptimizationRunStatus": ["demoCalculated", "noFeasibleSolution", "failedDemo"],
    "NotificationCategory": [
        "tripAssignment", "bookingUpdate", "pickupReminder", "delay", "routeChange",
        "serviceNotice", "incident", "billing", "shift", "system",
    ],
    "TicketCategory": ["trip", "booking", "driver", "vehicle", "app", "billing", "safety", "other"],
    "TicketStatus": ["open", "inProgress", "resolved", "closed"],
    "TransportPreference": ["noPreference", "evPreferred", "sedanPreferred", "shuttlePreferred"],
    "RouteVariant": ["ringRoad", "internalRoads"],
    "BillingCycle": ["weekly", "monthly"],
    "InvoiceStatus": ["generated", "reviewed", "paidDemo", "disputed"],
    "SelfDeclaredGender": ["notSpecified", "female", "male", "nonBinary", "preferNotToSay"],
    "FuelType": ["electric", "petrol", "diesel", "cng", "hybrid"],
    "ContractStatus": ["draft", "active", "suspended", "terminated"],
    "ResourceType": ["vehicle", "driver"],
    "AuditOutcome": ["success", "rejected", "failure"],
    "RevisionStatus": ["proposed", "applied", "dismissed", "superseded"],
    "TrafficLevel": ["free", "moderate", "heavy", "severe"],
    "TrafficSource": ["simulated", "unavailable"],
    "AlcoholCheckPoint": ["shiftStart", "everyTrip"],
    "AlcoholResult": ["pass", "fail", "inconclusive"],
    "GeofenceKind": ["serviceArea", "routeCorridor", "restrictedZone"],
    "VehicleEventType": [
        "speeding", "geofenceExit", "restrictedEntry", "routeDeviation",
        "diagnosticFault", "harshBraking",
    ],
    "SosRecipient": [
        "fleetManager", "companyAdmin", "supportTeam", "police", "driver", "emergencyContact",
    ],
    "SosDispatchStatus": ["alertedInApp", "acknowledged", "integrationRequired", "disabled"],
}


def snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()


# --------------------------------------------------------------------------- #
# Spec classes
# --------------------------------------------------------------------------- #
_UNSET: Any = object()


@dataclass
class Col:
    key: str                      # JSON key (camelCase, as in the Dart toJson)
    kind: str = "text"            # text|int|num|bool|ts|jsonb
    null: bool = False
    enum: str | None = None
    fk: str | None = None         # referenced table (column id)
    default: Any = _UNSET         # python default; makes the field optional on input
    unique: bool = False
    min: float | None = None
    max: float | None = None
    name: str = ""
    soft: bool = False            # never create a DB foreign key (cross-scope reference)

    def __post_init__(self) -> None:
        self.name = self.name or snake(self.key)

    @property
    def required(self) -> bool:
        return not self.null and self.default is _UNSET


@dataclass
class Ids:
    key: str
    table: str
    col: str                      # column holding the value
    owner: str                    # column pointing at the parent
    fk: str | None = None         # FK target of `col`
    discr: tuple[str, str] | None = None  # (column, constant) to share a table
    enum: str | None = None
    kind: str = "ids"


@dataclass
class Objs:
    key: str
    table: str
    owner: str
    cols: list[Col]
    kind: str = "objs"


@dataclass
class Kv:
    key: str
    table: str
    owner: str
    kcol: str
    vcol: str
    vkind: str = "int"
    kind: str = "kv"


@dataclass
class Derived:
    key: str
    sql: str                      # SELECT ... WHERE <owner> = ANY(%s) returning (owner, value)
    kind: str = "derived"


@dataclass
class Entity:
    table: str
    prefix: str
    cols: list[Col]
    lists: list = field(default_factory=list)
    search: list[str] = field(default_factory=list)
    sort: str = "id"
    checks: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)   # extra column groups / raw
    unique_nonempty: list[str] = field(default_factory=list)
    has_created_at: bool = False
    # Whether API clients may create/replace/delete. (all True except singletons)
    singleton: bool = False
    home: str = "company"       # fleet | company | both
    resource: str = ""          # API/resource key when it differs from the table name
    pin: str = ""               # always qualify the table with this schema (fleet_workspace)

    @property
    def key(self) -> str:
        return self.resource or self.table

    def col(self, key: str) -> Col | None:
        for c in self.cols:
            if c.key == key:
                return c
        return None

    @property
    def col_by_key(self) -> dict[str, Col]:
        return {c.key: c for c in self.cols}


def C(key, kind="text", **kw) -> Col:
    return Col(key, kind, **kw)


def T(key, **kw):  # required text
    return Col(key, "text", **kw)


def TS(key, **kw):
    return Col(key, "ts", **kw)


# Optional strings the Dart models default to ''.
def S(key, **kw):
    kw.setdefault("default", "")
    return Col(key, "text", **kw)


def B(key, default=False, **kw):
    return Col(key, "bool", default=default, **kw)


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
ENTITIES: dict[str, Entity] = {}


def add(e: Entity) -> Entity:
    ENTITIES[e.key] = e
    return e


add(Entity(
    "employers", "C",
    [
        T("code", unique=True), T("name"),
        C("status", enum="RecordStatus", default="active"),
        T("currency", default="INR"),
        C("maximumArrivalMinutes", "int", default=570, min=0, max=1440),
        C("defaultDistanceRate", "num", default=18, min=0),
        C("preferredEvPercentage", "int", default=50, min=0, max=100),
        C("luxuryPreference", enum="LuxuryClass", default="standard"),
        C("costPriority", "int", default=50, min=0, max=100),
        C("punctualityPriority", "int", default=50, min=0, max=100),
        B("securityGuardPolicyEnabled"), B("allowSharedPooling", True),
        B("allowCrossEmployerPooling"),
        C("pickupToleranceMinutes", "int", default=7, min=0, max=120),
        C("billingCycle", enum="BillingCycle", default="weekly"),
        B("billingActive", True),
        C("additionalChargePerTrip", "num", default=0, min=0),
        S("contactName"), S("contactEmail"), S("address"),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    lists=[
        Ids("vehicleTypePreferences", "employer_vehicle_type_prefs", "vehicle_type",
            "employer_id", enum="VehicleType"),
        Objs("shifts", "employer_shifts", "employer_id", [
            T("id", name="shift_key"), S("label"),
            C("startMinutes", "int", min=0, max=1440), C("endMinutes", "int", min=0, max=1440),
        ]),
    ],
    search=["code", "name", "contact_name", "address"], sort="code", has_created_at=True,
))

add(Entity(
    "locations", "L",
    [
        T("code", unique=True), T("name"),
        C("type", enum="LocationType", default="pickup"),
        S("address"), C("demoX", "num", default=0), C("demoY", "num", default=0),
        C("geofenceRadiusM", "int", null=True, min=0),
        C("employerId", fk="employers", null=True),
        B("active", True), S("notes"),
        C("capacity", "int", null=True, min=0),
        C("latitude", "num", null=True, min=-90, max=90),
        C("longitude", "num", null=True, min=-180, max=180),
        B("fleetOwned"),
    ],
    search=["code", "name", "address"], sort="code",
    indexes=["employer_id", "type"],
))

add(Entity(
    "vehicles", "V",
    [
        T("vehicleCode", unique=True), S("fictionalRegistration"),
        C("type", enum="VehicleType", default="sedan"),
        C("propulsion", enum="Propulsion", default="ice"),
        C("totalSeats", "int", default=4, min=2, max=60),
        C("luxuryClass", enum="LuxuryClass", default="standard"),
        C("status", enum="VehicleStatus", default="available"),
        C("odometerKm", "num", default=0, min=0),
        C("batteryOrFuelPercent", "int", null=True, min=0, max=100),
        C("assignedDriverId", fk="drivers", null=True),
        C("assignedEmployerId", fk="employers", null=True),
        B("requiresSecurityGuard"), B("securityGuardAssigned"), S("notes"),
        TS("lastInspectionAt", null=True),
        C("homeParkingId", fk="locations", null=True),
        S("model"), S("rcNumber"), S("engineNumber"), S("color"),
        C("fuelType", enum="FuelType", null=True),
        B("active", True),
    ],
    lists=[Ids("diagnosticCodes", "vehicle_diagnostic_codes", "code", "vehicle_id")],
    search=["vehicle_code", "fictional_registration", "model"], sort="vehicle_code",
    unique_nonempty=["fictional_registration", "rc_number", "engine_number"],
    indexes=["status", "assigned_driver_id", "assigned_employer_id", "home_parking_id"],
))

add(Entity(
    "drivers", "DR",
    [
        T("driverCode", unique=True), T("name"), S("fictionalContact"),
        C("status", enum="DriverStatus", default="offDuty"),
        C("assignedVehicleId", fk="vehicles", null=True),
        C("shiftStartMinutes", "int", default=360, min=0, max=1440),
        C("shiftEndMinutes", "int", default=840, min=0, max=1440),
        C("attendanceStatus", enum="AttendanceStatus", default="notCheckedIn"),
        C("completedTripCount", "int", default=0, min=0),
        C("safetyChecklistStatus", enum="SafetyChecklistStatus", default="pending"),
        T("licenceClass", default="LMV (demo)"), S("notes"), S("licenceNumber"),
        TS("licenceExpiry", null=True), S("email"), S("address"),
        C("gender", enum="SelfDeclaredGender", default="notSpecified"),
        B("active", True),
    ],
    search=["driver_code", "name", "licence_number"], sort="driver_code",
    unique_nonempty=["licence_number"], indexes=["status", "assigned_vehicle_id"],
))

add(Entity(
    "employees", "E",
    [
        T("employeeCode", unique=True), C("employerId", fk="employers"), T("name"),
        S("fictionalEmail"), S("fictionalPhone"),
        C("pickupLocationId", fk="locations"), C("destinationId", fk="locations"),
        C("requiredArrivalMinutes", "int", default=540, min=0, max=1440),
        S("shiftLabel"),
        C("transportPreference", enum="TransportPreference", default="noPreference"),
        C("status", enum="RecordStatus", default="active"),
        B("optionalDemoPasswordEnabled"), B("guardEscortRequired"), S("notes"),
        C("shiftStartMinutes", "int", null=True, min=0, max=1440),
        C("shiftEndMinutes", "int", null=True, min=0, max=1440),
        C("pickupToleranceMinutes", "int", null=True, min=0, max=120),
        C("gender", enum="SelfDeclaredGender", default="notSpecified"),
    ],
    lists=[Kv("travelPreferences", "employee_travel_preferences", "employee_id",
              "pref_key", "level", "int")],
    search=["employee_code", "name", "fictional_email"], sort="employee_code",
    indexes=["employer_id", "status", "pickup_location_id", "destination_id"],
))

add(Entity(
    "optimization_configs", "OC",
    [
        C("employerId", fk="employers", null=True), T("name"), B("enabled", True),
        C("costWeight", "num", default=0, min=0), C("punctualityWeight", "num", default=0, min=0),
        C("distanceWeight", "num", default=0, min=0), C("travelTimeWeight", "num", default=0, min=0),
        C("utilizationWeight", "num", default=0, min=0), C("evPreferenceWeight", "num", default=0, min=0),
        C("customerPreferenceWeight", "num", default=0, min=0), C("safetyWeight", "num", default=0, min=0),
        C("guardRequirementWeight", "num", default=0, min=0), C("detourWeight", "num", default=0, min=0),
        C("pickupProximityWeight", "num", default=0, min=0),
        C("employeePreferenceWeight", "num", default=0, min=0),
        C("maximumArrivalMinutes", "int", default=570, min=0, max=1440),
        C("minimumBufferMinutes", "int", default=0, min=0),
        C("minimumEvPercentage", "int", default=0, min=0, max=100),
        C("activeParameters", "jsonb", default={}),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    lists=[
        Ids("allowedVehicleTypes", "optimization_config_vehicle_types", "vehicle_type",
            "config_id", enum="VehicleType"),
        Kv("additionalParameters", "optimization_config_parameters", "config_id",
           "param_key", "param_value", "num"),
    ],
    search=["name"], sort="name", has_created_at=True, indexes=["employer_id"],
))

add(Entity(
    "optimization_runs", "OR",
    [
        T("reference", unique=True), C("configurationId", fk="optimization_configs"),
        C("employerId", fk="employers", null=True),
        TS("createdAt", default=None, null=True), TS("serviceDate"),
        C("status", enum="OptimizationRunStatus", default="demoCalculated"),
        S("inputSummary"), S("outputSummary"),
        C("totalDistanceKm", "num", default=0, min=0), C("estimatedMinutes", "int", default=0, min=0),
        C("estimatedCost", "num", default=0, min=0), C("onTimeScore", "num", default=0),
        C("vehicleUtilization", "num", default=0), C("evAllocationPercentage", "num", default=0),
        C("score", "num", default=0), B("isSimulation"),
        TS("appliedAt", null=True), TS("rejectedAt", null=True), S("rejectionReason", null=True, default=None),
        # Variable-length lists of free-text explanations: genuinely schemaless.
        C("warnings", "jsonb", default=[]), C("explanation", "jsonb", default=[]),
        C("hardConstraintViolations", "jsonb", default=[]), C("alternatives", "jsonb", default=[]),
        C("remedies", "jsonb", default=[]), C("preferenceSummary", "jsonb", default=[]),
        C("constraintsEvaluated", "jsonb", default=[]), C("weightsSnapshot", "jsonb", default={}),
    ],
    lists=[
        Ids("employeeIds", "optimization_run_employees", "employee_id", "run_id", fk="employees"),
        Ids("candidateVehicleIds", "optimization_run_candidates", "resource_id", "run_id",
            discr=("resource_type", "vehicle")),
        Ids("candidateDriverIds", "optimization_run_candidates", "resource_id", "run_id",
            discr=("resource_type", "driver")),
        Ids("appliedTripIds", "optimization_run_applied_trips", "trip_id", "run_id"),
        Objs("plans", "optimization_run_plans", "run_id", [
            C("vehicleId", fk="vehicles"), C("driverId", fk="drivers", null=True),
            C("employeeIds", "jsonb", default=[]), C("stops", "jsonb", default=[]),
            C("distanceKm", "num", default=0), C("durationMinutes", "int", default=0),
            C("estimatedCost", "num", default=0), TS("departure", null=True),
            TS("latestArrival", null=True), TS("deadline", null=True),
            C("passengerCapacity", "int", default=0), C("guardSeats", "int", default=0),
            C("variant", enum="RouteVariant", default="ringRoad"),
            C("startLocationId", fk="locations", null=True, soft=True),
            C("deadheadKm", "num", default=0), S("guardSource", null=True, default=None),
            C("totalSeats", "int", default=0),
        ]),
        Objs("scoreBreakdown", "optimization_run_scores", "run_id", [
            T("key", name="component_key"), T("label"), C("weight", "num", default=0),
            C("normalizedPenalty", "num", default=0), S("detail"),
        ]),
    ],
    search=["reference"], sort="created_at", has_created_at=True,
    indexes=["employer_id", "configuration_id", "service_date"],
))

add(Entity(
    "routes", "R",
    [
        T("routeCode", unique=True), T("name"), C("employerId", fk="employers", null=True),
        C("estimatedDistanceKm", "num", default=0, min=0),
        C("estimatedDurationMinutes", "int", default=0, min=0),
        C("status", enum="RouteStatus", default="draft"),
        C("optimizationRunId", fk="optimization_runs", null=True),
        C("variant", enum="RouteVariant", default="ringRoad"),
    ],
    lists=[Derived("stopIds", "SELECT route_id, id FROM route_stops WHERE route_id = ANY(%s) ORDER BY sequence")],
    search=["route_code", "name"], sort="route_code", indexes=["employer_id", "status"],
))

add(Entity(
    "route_stops", "RS",
    [
        C("routeId", fk="routes"), C("locationId", fk="locations"),
        C("sequence", "int", min=0), C("kind", enum="StopKind"),
        TS("plannedArrival", null=True), TS("actualArrival", null=True), TS("departedAt", null=True),
        C("expectedBoardingCount", "int", default=0, min=0),
        C("expectedAlightingCount", "int", default=0, min=0),
        C("status", enum="StopStatus", default="upcoming"),
        S("skipReason", null=True, default=None), S("skipAuthorizedBy", null=True, default=None),
        S("issueNote", null=True, default=None),
    ],
    lists=[
        Ids("boardingEmployeeIds", "route_stop_passengers", "employee_id", "stop_id",
            fk="employees", discr=("direction", "board")),
        Ids("alightingEmployeeIds", "route_stop_passengers", "employee_id", "stop_id",
            fk="employees", discr=("direction", "alight")),
    ],
    sort="route_id", indexes=["route_id", "location_id"],
))

add(Entity(
    "trips", "TR-",
    [
        T("tripCode", unique=True), C("employerId", fk="employers"),
        C("assignedDriverId", fk="drivers", null=True), C("assignedVehicleId", fk="vehicles", null=True),
        C("routeId", fk="routes", null=True),
        TS("serviceDate"), TS("scheduledStart"), TS("actualStart", null=True),
        TS("scheduledArrival", null=True), TS("actualArrival", null=True),
        C("originLocationId", fk="locations"), C("destinationLocationId", fk="locations"),
        C("distanceKm", "num", null=True, min=0), C("estimatedDurationMinutes", "int", null=True, min=0),
        C("actualDurationMinutes", "int", null=True, min=0),
        C("status", enum="TripStatus", default="draft"),
        C("optimizationRunId", fk="optimization_runs", null=True),
        S("delayReason", null=True, default=None), S("completionNotes", null=True, default=None),
        S("completionException", null=True, default=None),
        C("billableStatus", enum="BillableStatus", default="notYetBillable"),
        B("guardRequired"), S("instructions"),
        C("startOdometerKm", "num", null=True, min=0), C("endOdometerKm", "num", null=True, min=0),
        C("inspectionId", fk="inspections", null=True),
        S("cancellationReason", null=True, default=None), S("guardReference", null=True, default=None),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    lists=[
        Ids("employeeIds", "trip_passengers", "employee_id", "trip_id", fk="employees",
            discr=("state", "assigned")),
        Ids("boardedPassengerIds", "trip_passengers", "employee_id", "trip_id", fk="employees",
            discr=("state", "boarded")),
        Ids("alightedPassengerIds", "trip_passengers", "employee_id", "trip_id", fk="employees",
            discr=("state", "alighted")),
        Ids("noShowPassengerIds", "trip_passengers", "employee_id", "trip_id", fk="employees",
            discr=("state", "noShow")),
        Ids("absentEmployeeIds", "trip_passengers", "employee_id", "trip_id", fk="employees",
            discr=("state", "absent")),
        Ids("orderedStopIds", "trip_stops", "location_id", "trip_id", fk="locations"),
        Objs("assignmentHistory", "trip_assignment_history", "trip_id", [
            TS("at"), C("actorRole", enum="UserRole"),
            C("driverId", fk="drivers", null=True), C("vehicleId", fk="vehicles", null=True),
            S("reason"),
        ]),
    ],
    search=["trip_code", "delay_reason"], sort="scheduled_start", has_created_at=True,
    checks=["(actual_arrival IS NULL OR actual_start IS NULL OR actual_arrival >= actual_start)"],
    indexes=["employer_id", "status", "assigned_driver_id", "assigned_vehicle_id",
             "service_date", "route_id"],
))

add(Entity(
    "bookings", "B",
    [
        T("reference", unique=True), C("employeeId", fk="employees"),
        C("tripId", fk="trips", null=True),
        C("requestedPickupLocationId", fk="locations"), C("requestedDestinationId", fk="locations"),
        TS("requestedDate"), C("requestedArrivalMinutes", "int", min=0, max=1440),
        C("status", enum="BookingStatus", default="requested"), S("notes"),
        C("preference", enum="TransportPreference", default="noPreference"),
        S("decisionReason", null=True, default=None),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    search=["reference", "notes"], sort="created_at", has_created_at=True,
    indexes=["employee_id", "trip_id", "status", "requested_date"],
))

add(Entity(
    "passenger_events", "PE",
    [
        C("tripId", fk="trips"), C("employeeId", fk="employees"),
        C("stopId", fk="route_stops", null=True), C("eventType", enum="PassengerEventType"),
        C("verificationMethod", enum="VerificationMethod"),
        C("verificationStatus", enum="VerificationStatus"),
        TS("eventTime"), S("note"), S("authorizedBy", null=True, default=None),
    ],
    sort="event_time", indexes=["trip_id", "employee_id"],
))

add(Entity(
    "shifts", "S",
    [
        C("driverId", fk="drivers"), TS("shiftDate"), TS("scheduledStart"), TS("scheduledEnd"),
        TS("actualCheckIn", null=True), TS("actualCheckOut", null=True),
        C("status", enum="ShiftStatus", default="scheduled"),
        C("regularizationStatus", enum="RegularizationStatus", null=True),
        S("regularizationReason", null=True, default=None), S("notes"),
        S("handoverNotes", null=True, default=None),
        C("handoverParkingId", fk="locations", null=True),
        S("endException", null=True, default=None), C("endOdometerKm", "num", null=True, min=0),
    ],
    sort="shift_date", indexes=["driver_id", "shift_date"],
    checks=["(scheduled_end >= scheduled_start)"],
))

add(Entity(
    "inspections", "IN",
    [
        C("vehicleId", fk="vehicles"), C("driverId", fk="drivers"), C("tripId", fk="trips", null=True),
        TS("date"), S("notes"), S("photoPath", null=True, default=None),
        C("overallStatus", enum="InspectionOutcome"), TS("submittedAt", null=True),
        C("defectIncidentId", fk="incidents", null=True),
    ],
    lists=[Objs("items", "inspection_items", "inspection_id", [
        T("key", name="item_key"), T("label"), B("critical"),
        C("result", enum="CheckResult"), S("note"),
    ])],
    sort="date", indexes=["vehicle_id", "driver_id", "trip_id"],
))

add(Entity(
    "incidents", "INC",
    [
        T("reference", unique=True), C("type", enum="IncidentType"),
        C("tripId", fk="trips", null=True), C("driverId", fk="drivers", null=True),
        C("vehicleId", fk="vehicles", null=True), C("locationId", fk="locations", null=True, soft=True),
        T("description"), C("severity", enum="IncidentSeverity", default="medium"),
        C("status", enum="IncidentStatus", default="draft"),
        C("reportedByRole", enum="UserRole"), S("reportedById", null=True, default=None),
        S("resolutionNotes", null=True, default=None), S("demoLocationLabel", null=True, default=None),
        B("replacementRequested"), B("isSos"),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    search=["reference", "description"], sort="created_at", has_created_at=True,
    indexes=["trip_id", "driver_id", "vehicle_id", "status", "is_sos"],
))

add(Entity(
    "invoices", "INV",
    [
        T("reference", unique=True), C("employerId", fk="employers"),
        C("cycle", enum="BillingCycle"), TS("periodStart"), TS("periodEnd"),
        C("sequence", "int", default=1, min=1),
        C("distanceKm", "num", default=0, min=0), C("distanceCharges", "num", default=0),
        C("additionalCharges", "num", default=0), C("adjustments", "num", default=0),
        C("total", "num", default=0), C("status", enum="InvoiceStatus", default="generated"),
        TS("generatedAt"), S("notes"), S("disputeReason", null=True, default=None),
        TS("reviewedAt", null=True),
    ],
    lists=[
        Derived("billingRecordIds",
                "SELECT invoice_id, id FROM billing_records WHERE invoice_id = ANY(%s) ORDER BY id"),
        Ids("tripIds", "invoice_trips", "trip_id", "invoice_id", fk="trips"),
    ],
    search=["reference"], sort="generated_at", indexes=["employer_id", "status"],
    checks=["(period_end >= period_start)"],
))

add(Entity(
    "billing_records", "BR",
    [
        C("employerId", fk="employers"), C("tripId", fk="trips"), T("tripCode"),
        TS("completedAt"), C("billableDistanceKm", "num", default=0, min=0),
        C("ratePerKm", "num", default=0, min=0), C("additionalCharges", "num", default=0),
        C("adjustment", "num", default=0), C("totalAmount", "num", default=0),
        C("status", enum="BillingStatus", default="draft"), S("calculationNotes"),
        S("disputeReason", null=True, default=None),
        TS("createdAt", default=None, null=True), TS("reviewedAt", null=True),
        C("invoiceId", fk="invoices", null=True),
    ],
    search=["trip_code"], sort="completed_at", has_created_at=True,
    indexes=["employer_id", "trip_id", "invoice_id", "status"],
))

add(Entity(
    "notifications", "N",
    [
        C("recipientRole", enum="UserRole"), S("recipientId", null=True, default=None),
        T("title"), T("body"), C("category", enum="NotificationCategory", default="system"),
        TS("createdAt", default=None, null=True), TS("readAt", null=True),
        S("relatedEntityType", null=True, default=None), S("relatedEntityId", null=True, default=None),
        B("demoOnly"),
    ],
    search=["title", "body"], sort="created_at", has_created_at=True,
    indexes=["recipient_role", "recipient_id", "read_at"],
))

add(Entity(
    "support_tickets", "T",
    [
        T("reference", unique=True), C("createdByRole", enum="UserRole"), T("createdById"),
        C("category", enum="TicketCategory", default="other"), T("subject"), T("description"),
        C("relatedTripId", fk="trips", null=True),
        C("status", enum="TicketStatus", default="open"), S("response", null=True, default=None),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    search=["reference", "subject", "description"], sort="created_at", has_created_at=True,
    indexes=["created_by_id", "status"],
))

add(Entity(
    "audit_events", "AU",
    [
        C("actorRole", enum="UserRole"), S("actorId", null=True, default=None),
        T("action"), T("entityType"), T("entityId"), TS("timestamp", default=None, null=True),
        S("summary"), C("outcome", enum="AuditOutcome", default="success"),
        S("changes"), S("reason", null=True, default=None),
        C("related", "jsonb", default={}), B("simulated"),
    ],
    search=["action", "summary", "entity_id"], sort="timestamp", has_created_at=False,
    indexes=["entity_type", "entity_id", "actor_id", "timestamp"],
))

add(Entity(
    "user_settings", "US",
    [
        B("shareSimulatedLocation", True), B("pickupReminders", True), B("delayAlerts", True),
        B("serviceNotices", True), B("showPhoneToDriver"), B("largeText"),
    ],
    sort="id",
))

add(Entity(
    "contracts", "CT",
    [
        T("contractCode", unique=True), C("employerId", fk="employers"), S("documentRef"),
        TS("validFrom"), TS("validUntil"),
        C("status", enum="ContractStatus", default="draft"),
        C("agreedVehicleCount", "int", default=0, min=0),
        C("costPerVehicle", "num", default=0, min=0), T("currency", default="INR"),
        T("costPeriod", default="month"), B("hardVehicleLimit"),
        S("contactId", null=True, default=None), S("contactName"), S("notes"),
        TS("createdAt", default=None, null=True), TS("updatedAt", default=None, null=True),
    ],
    search=["contract_code", "document_ref"], sort="valid_from", has_created_at=True,
    indexes=["employer_id", "status"], checks=["(valid_until >= valid_from)"],
))

add(Entity(
    "fleet_allocations", "FA",
    [
        C("employerId", fk="employers"), C("resourceType", enum="ResourceType"),
        T("resourceId"),   # polymorphic (vehicle|driver): integrity enforced by the API layer
        C("windowStartMinutes", "int", min=0, max=1440),
        C("windowEndMinutes", "int", min=0, max=1440),
        S("label"), B("active", True), TS("createdAt", default=None, null=True),
    ],
    sort="employer_id", has_created_at=True, indexes=["employer_id", "resource_type", "resource_id"],
))

add(Entity(
    "route_revisions", "RV",
    [
        C("tripId", fk="trips"), TS("createdAt", default=None, null=True),
        T("scenarioKey"), T("scenarioLabel"), S("reason"),
        TS("previousDeparture"), TS("proposedDeparture"),
        C("delayMinutes", "int", default=0), B("feasible", True),
        C("status", enum="RevisionStatus", default="proposed"),
        C("violations", "jsonb", default=[]), S("recommendation"),
        TS("appliedAt", null=True), B("simulated", True),
    ],
    lists=[
        Ids("outsideToleranceEmployeeIds", "route_revision_employees", "employee_id",
            "revision_id", fk="employees"),
        Objs("stops", "route_revision_stops", "revision_id", [
            T("stopId"), C("locationId", fk="locations"), TS("previous", null=True),
            TS("proposed", null=True), B("locked"), C("employeeIds", "jsonb", default=[]),
        ]),
    ],
    sort="created_at", has_created_at=True, indexes=["trip_id", "status"],
))

add(Entity(
    "traffic_state", "TS",
    [
        C("internalRoads", enum="TrafficLevel", default="free"),
        C("ringRoad", enum="TrafficLevel", default="free"),
        C("source", enum="TrafficSource", default="simulated"),
        TS("updatedAt", default=None, null=True), S("note"),
    ],
    sort="id", singleton=True,
))

add(Entity(
    "fleet_policy", "FP",
    [
        B("alcoholMonitoringEnabled"), C("alcoholThresholdMg", "num", default=0, min=0),
        C("alcoholCheckPoint", enum="AlcoholCheckPoint", default="shiftStart"),
        C("speedLimitKmh", "int", default=60, min=1), B("speedAlertsEnabled", True),
        B("geofenceAlertsEnabled", True), C("routeCorridorKm", "num", default=1, min=0),
        B("autoMatchAdhoc"), C("maxAddedDetourMinutes", "int", default=10, min=0),
        C("carbonCreditPricePerTonne", "num", default=0, min=0),
        C("baselineCo2GramsPerKm", "num", default=0, min=0),
        C("baselineSpeedKmh", "num", default=0, min=0),
        C("baselineCostPerKm", "num", default=0, min=0),
        TS("updatedAt", default=None, null=True),
    ],
    lists=[Ids("sosRecipients", "fleet_policy_sos_recipients", "recipient", "policy_id",
               enum="SosRecipient")],
    sort="id", singleton=True,
))

add(Entity(
    "alcohol_tests", "AT",
    [
        C("driverId", fk="drivers"), C("tripId", fk="trips", null=True),
        C("vehicleId", fk="vehicles", null=True), TS("testedAt"),
        C("readingMg", "num", min=0), C("thresholdMg", "num", min=0),
        C("result", enum="AlcoholResult"), S("method"), S("note"),
        S("clearedBy", null=True, default=None), TS("clearedAt", null=True),
        C("incidentId", fk="incidents", null=True),
    ],
    sort="tested_at", indexes=["driver_id", "trip_id", "tested_at"],
))

add(Entity(
    "geofences", "G",
    [
        T("name"), C("kind", enum="GeofenceKind"),
        C("centerX", "num", default=0), C("centerY", "num", default=0),
        C("radiusKm", "num", default=1, min=0), B("active", True), S("notes"),
        TS("createdAt", default=None, null=True),
    ],
    lists=[Ids("vehicleIds", "geofence_vehicles", "vehicle_id", "geofence_id", fk="vehicles")],
    search=["name"], sort="name", has_created_at=True,
))

add(Entity(
    "vehicle_events", "VE",
    [
        C("vehicleId", fk="vehicles"), C("tripId", fk="trips", null=True),
        C("driverId", fk="drivers", null=True), C("geofenceId", fk="geofences", null=True),
        C("type", enum="VehicleEventType"), TS("occurredAt"), S("description"),
        C("value", "num", null=True), C("limit", "num", null=True),
        C("x", "num", null=True), C("y", "num", null=True),
        TS("acknowledgedAt", null=True), C("incidentId", fk="incidents", null=True),
        B("simulated", True),
    ],
    sort="occurred_at", indexes=["vehicle_id", "trip_id", "type", "occurred_at"],
))

add(Entity(
    "sos_dispatches", "SD",
    [
        C("incidentId", fk="incidents"), C("recipient", enum="SosRecipient"),
        C("status", enum="SosDispatchStatus"), S("channel"),
        TS("createdAt", default=None, null=True), TS("acknowledgedAt", null=True),
        S("acknowledgedBy", null=True, default=None), S("note"),
    ],
    sort="created_at", has_created_at=True, indexes=["incident_id", "status"],
))

# Entities where the client may supply *any* id (singletons, 'role:id' keys)
CLIENT_ID_ENTITIES = {"user_settings", "traffic_state", "fleet_policy"}

# Resources whose rows carry an employer_id (used for employer-admin scoping).
for _e in ENTITIES.values():
    _e.scoped_by_employer = any(c.name == "employer_id" for c in _e.cols)  # type: ignore[attr-defined]
ENTITIES["employers"].scoped_by_employer = False  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Homes: which schema owns each entity
# --------------------------------------------------------------------------- #
import dataclasses  # noqa: E402

FLEET_HOME = ["vehicles", "drivers", "shifts", "inspections", "vehicle_events", "alcohol_tests",
              "geofences", "fleet_policy", "traffic_state", "optimization_configs", "contracts",
              "fleet_allocations"]
BOTH_HOME = ["notifications", "support_tickets", "audit_events", "user_settings", "incidents",
             "sos_dispatches"]
for _n in FLEET_HOME:
    ENTITIES[_n].home = "fleet"
for _n in BOTH_HOME:
    ENTITIES[_n].home = "both"
# everything else (employers, locations, employees, routes, trips, bookings, ...) is "company"

# Parking lots and depots are FLEET assets: same columns as locations, but stored in
# fleet_workspace.locations. The app's single "locations" list is split by type.
ENTITIES["parking_locations"] = dataclasses.replace(
    ENTITIES["locations"], resource="parking_locations", home="fleet", pin="fleet_workspace",
    indexes=["type"])
PARKING_TYPES = ("parking", "depot")

# Resource keys the client (Flutter) uses -> entities; 'locations' is routed by type.
KINDS = ("fleet", "company")


def entities_for(kind: str) -> list[Entity]:
    """Entities that have a physical table in a schema of this kind."""
    return [e for e in ENTITIES.values() if e.home in (kind, "both")]


def table_kinds() -> dict[str, set[str]]:
    """physical table name -> kinds of schema that contain it (children follow parents)."""
    out: dict[str, set[str]] = {}
    for e in ENTITIES.values():
        ks = {"fleet", "company"} if e.home == "both" else {e.home}
        out.setdefault(e.table, set()).update(ks)
        for l in e.lists:
            if not isinstance(l, Derived):
                out.setdefault(l.table, set()).update(ks)
    return out


TABLE_KINDS = table_kinds()


def fk_target(kind: str, table: str, soft: bool = False) -> str | None:
    """Qualified FK target for a reference to ``table`` from a schema of ``kind``.

    None means "no database FK" (soft reference).
    """
    if soft:
        return None
    ks = TABLE_KINDS.get(table, set())
    if kind == "fleet" and "company" in ks and table != "locations":
        # a fleet row may point at a row (e.g. an incident) that was recorded in a company schema:
        # the reference cannot be a database FK (parking locations always live in the fleet schema)
        return None
    if kind in ks:
        return f'"{table}"'
    if kind == "company" and "fleet" in ks:
        return f'"fleet_workspace"."{table}"'
    return None


# Fleet-level assets are not scoped by employer; the parking entity has no employer.
ENTITIES["parking_locations"].scoped_by_employer = False  # type: ignore[attr-defined]
