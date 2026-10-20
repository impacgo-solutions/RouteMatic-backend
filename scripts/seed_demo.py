"""Demo data seeder: three synthetic demo companies + Fleet Workspace demo data.

    # preview only (reads, never writes); check existing demo data
    python -m scripts.seed_demo --database routematic_test_demo --verify
    python -m scripts.seed_demo --database routematic_test_demo --dry-run
    # create the demo database's schemas if needed, then seed (development / demo databases only)
    DEMO_SEED_PASSWORD=... python -m scripts.seed_demo --database routematic_test_demo --init --yes-demo-data

Safety
* Runs only with ``--yes-demo-data`` (or ``ROUTEMATIC_DEMO_SEED=1``); ``--dry-run`` needs neither.
* Development / demo databases are the ones named ``routematic_test_<name>``. Any other database (the real
  ``Routematic`` included) is refused unless ``--confirm-database <exact name>`` is given — get the
  owner's approval first. Protected databases (``impac_test_dev`` ...) are always refused.
* Never deletes, truncates or resets anything. Existing records are never overwritten: a record whose id
  already exists is reported as "existing" and left as it is. Re-running creates nothing new.
* Every write goes through the application's own API (in-process): company registration
  (``POST /t/fleet/companies``), sync push (permissions, row ownership, trip state machine, parking
  capacity, allocation overlap, references), the login endpoints and Fleet Manager management. The only
  direct database write is the demo Fleet Admin account, made the same way as
  ``scripts/create_fleet_manager.py`` (there is no API for creating Fleet Admins).
* Demo marker: every id / code starts with ``DEMO-``, company names end in "(Demo)", accounts use the
  reserved ``demo.routematic.test`` domain, notes carry ``[DEMO DATA routematic-demo-v1]`` and each
  workspace gets a ``demo.seed`` audit event.
* Passwords: one demo password from the ``DEMO_SEED_PASSWORD`` environment variable (never printed, stored
  only as PBKDF2 hashes by the API).
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

MARK = "routematic-demo-v1"
NOTE = f"[DEMO DATA {MARK}] synthetic record"
DOMAIN = "demo.routematic.test"
FLEET_ADMIN_EMAIL = f"fleet.admin@{DOMAIN}"
FLEET_MANAGER_EMAIL = f"fleet.manager@{DOMAIN}"
DEMO_DB_PREFIX = "routematic_test_"
# The demo companies operate in Bengaluru: business times are India Standard Time (no daylight saving).
# The app sends UTC instants (local time converted), so the seeder does the same.
TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))


class SeedRefused(RuntimeError):
    """The target is not a database the seeder may write to."""


# --------------------------------------------------------------------------- target checks
def check_target(name: str, confirm: str | None, protected: frozenset[str]) -> str:
    """Return 'demo' (a development / demo database) or 'confirmed'; raise SeedRefused otherwise."""
    if not name:
        raise SeedRefused("No database name configured.")
    if name.lower() in protected:
        raise SeedRefused(f"'{name}' is a protected database; the seeder never touches it.")
    if name.startswith(DEMO_DB_PREFIX):
        return "demo"
    if confirm == name:
        return "confirmed"
    raise SeedRefused(
        f"'{name}' is not a development/demo database (expected '{DEMO_DB_PREFIX}<name>'). It may hold real "
        f"data, so nothing was written. If the owner has approved demo data in it, re-run with "
        f"--confirm-database {name}.")


# --------------------------------------------------------------------------- synthetic data
COMPANIES = [
    dict(key="NST", code="DEMO-NST", slug="demo_northstar", name="Northstar Technologies (Demo)",
         cycle="weekly", rate=18.0, extra=25.0, ev=60, cost=45, punctual=70, office=("Northstar Tower",
         12.9698, 77.7499), domain=f"northstar.{DOMAIN}", window=(360, 1380),
         pickups=[("Maple Court Gate", 12.9846, 77.7210), ("Brookfield Arcade", 12.9622, 77.7178),
                  ("Kundan Lake Road", 12.9551, 77.7353), ("Siriva Layout", 12.9903, 77.7392),
                  ("Varthur Lake View", 12.9409, 77.7466), ("Hoodi Junction", 12.9913, 77.7151)]),
    dict(key="BPC", code="DEMO-BPC", slug="demo_bluepeak", name="BluePeak Consulting (Demo)",
         cycle="monthly", rate=20.0, extra=0.0, ev=40, cost=60, punctual=60, office=("BluePeak House",
         12.9352, 77.6245), domain=f"bluepeak.{DOMAIN}", window=(360, 1380),
         pickups=[("Ejipura Signal", 12.9440, 77.6292), ("Sarjapur Main Road Stop", 12.9172, 77.6402),
                  ("HSR Sector Gate", 12.9116, 77.6474), ("Domlur Inner Ring", 12.9601, 77.6387),
                  ("Madivala Market", 12.9226, 77.6174), ("Indiranagar Metro Gate", 12.9784, 77.6408)]),
    dict(key="GDS", code="DEMO-GDS", slug="demo_greenfield", name="Greenfield Digital Services (Demo)",
         cycle="weekly", rate=16.5, extra=15.0, ev=75, cost=50, punctual=65, office=("Greenfield Campus",
         13.0358, 77.5970), domain=f"greenfield.{DOMAIN}", window=(360, 1380),
         pickups=[("Sahakar Nagar Circle", 13.0627, 77.5864), ("Kodigehalli Gate", 13.0565, 77.5779),
                  ("RT Nagar Park", 13.0213, 77.5946), ("Thanisandra Cross", 13.0525, 77.6339),
                  ("Yelahanka Old Town", 13.0990, 77.5963), ("Nagawara Lake Road", 13.0418, 77.6214)]),
]
FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Isha", "Arjun", "Nisha", "Dev",
         "Tara", "Kiran", "Leela", "Neel", "Riya", "Samar", "Pooja", "Yash", "Zoya", "Aditi", "Manav",
         "Sana", "Varun", "Ira", "Jai", "Kavya", "Omkar", "Priya", "Rahul", "Saira", "Tanvi", "Uday",
         "Vani", "Ayaan", "Bhavna"]
LAST = ["Menon", "Rao", "Iyer", "Kulkarni", "Shetty", "Nair", "Bhat", "Desai", "Joshi", "Pillai", "Reddy",
        "Hegde"]
DRIVER_NAMES = ["Ravi Gowda", "Suresh Naik", "Imran Pasha", "Manjunath K", "Prakash Shenoy",
                "Anil Kamath", "Farhan Ali", "Gopal Das", "Harish Patil", "Joseph Dsouza", "Kumar Swamy",
                "Lokesh B", "Mahesh Urs", "Naveen T", "Om Prakash"]
SHIFTS = [dict(id="EARLY", label="Early 07:00-16:00", startMinutes=420, endMinutes=960),
          dict(id="MORNING", label="Morning 09:00-18:00", startMinutes=540, endMinutes=1080),
          dict(id="GENERAL", label="General 09:30-18:30", startMinutes=570, endMinutes=1110)]
# per company: (type, propulsion, fuel, seats, luxury)
VEHICLES = [("sedan", "ice", "petrol", 4, "standard"), ("mpv", "hybrid", "hybrid", 7, "premium"),
            ("sedan", "ev", "electric", 4, "standard"), ("van", "ice", "diesel", 10, "standard"),
            ("sedan", "ev", "electric", 4, "standard")]          # V5: in maintenance (spare)
PARKING = [("DEMO-PK-NORTH", "Demo North Parking Yard", "parking", 6, 13.0450, 77.6050),
           ("DEMO-PK-EAST", "Demo East Parking Yard", "parking", 6, 12.9750, 77.7300),
           ("DEMO-DEPOT-SOUTH", "Demo South Depot (EV charging, simulated)", "depot", 6, 12.9300, 77.6300)]
PARKING_OF = {"NST": "DEMO-PK-EAST", "BPC": "DEMO-DEPOT-SOUTH", "GDS": "DEMO-PK-NORTH"}


def km_xy(lat: float, lng: float) -> tuple[float, float]:
    """Plausible coordinates -> the app's schematic plane (km from a fixed origin)."""
    return round((lng - 77.50) * 108.9, 2), round((lat - 12.85) * 110.6, 2)


def ts(d: dt.date, minutes: int = 0) -> str:
    """Local business time (``TZ``) as an exact instant, like the app's ``toUtc()`` on upload."""
    return (dt.datetime(d.year, d.month, d.day, tzinfo=TZ) + dt.timedelta(minutes=minutes)).isoformat()


def r2(v: float) -> float:
    return float(f"{v:.2f}")


def billing_period(cycle: str, d: dt.date) -> tuple[dt.date, dt.date, str]:
    """Same boundaries as lib/domain/services/billing_cycle_service.dart (BillingPeriod.of)."""
    if cycle == "weekly":
        start = d - dt.timedelta(days=d.weekday())
        return start, start + dt.timedelta(days=6), f"{start:%Y%m%d}W"
    start = d.replace(day=1)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt - dt.timedelta(days=1), f"{start:%Y-%m}"


@dataclass
class Plan:
    """Everything the seeder wants to exist, grouped by workspace and phase."""
    fleet_assets: list = field(default_factory=list)     # (resource, id, data)
    fleet_ops: list = field(default_factory=list)
    companies: list = field(default_factory=list)        # dicts: registration + phases


def build_plan(anchor: dt.date) -> Plan:
    plan = Plan()
    today, tomorrow = anchor, anchor + dt.timedelta(days=1)
    for p in PARKING:
        x, y = km_xy(p[4], p[5])
        plan.fleet_assets.append(("locations", p[0], dict(
            code=p[0], name=p[1], type=p[2], capacity=p[3], latitude=p[4], longitude=p[5], demoX=x, demoY=y,
            address=f"{p[1]}, Bengaluru (fictional address)", fleetOwned=True, active=True, notes=NOTE)))
    dn = iter(DRIVER_NAMES)
    vehicle_ids_all = []
    for c in COMPANIES:
        k, code = c["key"], c["code"]
        ox, oy = km_xy(c["office"][1], c["office"][2])
        office_id = f"DEMO-{k}-OFC"
        locs = [("locations", office_id, dict(
            code=office_id, name=c["office"][0], type="office", employerId=code, latitude=c["office"][1],
            longitude=c["office"][2], demoX=ox, demoY=oy, geofenceRadiusM=200, active=True, notes=NOTE,
            address=f"{c['office'][0]}, Bengaluru (fictional address)"))]
        xy = {office_id: (ox, oy)}
        pickup_ids = []
        for i, (nm, lat, lng) in enumerate(c["pickups"], 1):
            lid = f"DEMO-{k}-PU{i}"
            pickup_ids.append(lid)
            xy[lid] = km_xy(lat, lng)
            locs.append(("locations", lid, dict(
                code=lid, name=nm, type="pickup", employerId=code, latitude=lat, longitude=lng,
                demoX=xy[lid][0], demoY=xy[lid][1], geofenceRadiusM=150, active=True, notes=NOTE,
                address=f"{nm}, Bengaluru (fictional address)")))
        ci = COMPANIES.index(c)
        emps = []

        def shift_of(i: int) -> dict:          # employees 1-6 early, 7-9 general, 10-12 morning shift
            return SHIFTS[0] if i < 6 else SHIFTS[2] if i < 9 else SHIFTS[1]
        for i in range(12):
            first, last = FIRST[ci * 12 + i], LAST[(i * 5 + ci) % len(LAST)]
            eid = f"DEMO-{k}-E{i + 1:02d}"
            sh = shift_of(i)
            emps.append(("employees", eid, dict(
                employeeCode=eid, employerId=code, name=f"{first} {last}",
                fictionalEmail=f"{first.lower()}.{last.lower()}@{c['domain']}",
                fictionalPhone=f"+91 00000 {ci + 1}{i + 1:03d}", pickupLocationId=pickup_ids[i % 6],
                destinationId=office_id, requiredArrivalMinutes=sh["startMinutes"], shiftLabel=sh["label"],
                shiftStartMinutes=sh["startMinutes"], shiftEndMinutes=sh["endMinutes"],
                pickupToleranceMinutes=7, transportPreference=["noPreference", "evPreferred",
                                                               "sedanPreferred"][i % 3],
                gender=["female", "male", "notSpecified"][i % 3], status="active", notes=NOTE,
                travelPreferences={"travelTime": 2, "reliability": 3, "detour": i % 4})))
        # fleet assets of this company: 5 vehicles (V5 spare, maintenance), 5 drivers (D5 spare, off duty)
        vids = [f"DEMO-{k}-V{i}" for i in range(1, 6)]
        dids = [f"DEMO-{k}-D{i}" for i in range(1, 6)]
        vehicle_ids_all += vids
        fleet_rows = []
        # trips (computed first: vehicle / driver status follows them)
        trips, routes, stops, bookings, billing, invoices = [], [], [], [], [], []
        start_date, end_date, period_code = billing_period(c["cycle"], billing_period(c["cycle"], today)[0]
                                                           - dt.timedelta(days=1))
        if c["cycle"] == "weekly":
            past = [start_date + dt.timedelta(days=n) for n in (1, 2, 3, 4)]
        else:
            past = [end_date - dt.timedelta(days=n) for n in (2, 1, 0)] + [end_date - dt.timedelta(days=5)]
        past += [today - dt.timedelta(days=2), today - dt.timedelta(days=1)]
        specs = []      # (target status, date, driver idx|None, emp idxs, absent idxs, shift)
        for n, d in enumerate(past):
            specs.append(("completed", d, n % 4, [(3 * n + j) % 12 for j in range(3)], []))
        specs += [("inProgress", today, 0, [0, 1, 2], []), ("delayed", today, 1, [3, 4, 5], []),
                  ("assigned", today, 2, [6, 7, 8], []), ("cancelled", today, None, [9, 10, 11], []),
                  ("planned", tomorrow, None, [0, 1], [2]), ("planned", tomorrow, None, [3, 4, 5], [])]
        # every trip runs for its passengers' own shift (each group of three shares one)
        specs = [(*sp, shift_of(sp[3][0])) for sp in specs]
        emp_ids = [e[1] for e in emps]
        emp_pick = {e[1]: e[2]["pickupLocationId"] for e in emps}
        odo = [18250.0 + 3100 * i for i in range(5)]
        completed_by_driver = defaultdict(int)
        inv_records = []
        for n, (status, d, di, eix, absent, sh) in enumerate(specs, 1):
            tid, rid = f"DEMO-{k}-T{n:02d}", f"DEMO-{k}-R{n:02d}"
            riders = [emp_ids[i] for i in eix]
            seq = []
            for e in riders:
                if emp_pick[e] not in seq:
                    seq.append(emp_pick[e])
            pts = [xy[s] for s in seq] + [xy[office_id]]
            dist = round(sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) * 1.3 + 1.5, 1)
            dur = int(round(dist / 24 * 60)) + 4 * len(seq)
            arrive = sh["startMinutes"] - 10
            depart = arrive - dur
            drv = dids[di] if di is not None else None
            veh = vids[di] if di is not None else None
            # stops (planned times along the route)
            leg_min = [0]
            for i in range(len(pts) - 1):
                leg_min.append(leg_min[-1] + max(4, int(math.dist(pts[i], pts[i + 1]) * 1.3 / 24 * 60) + 3))
            stop_ids = []
            for si, loc in enumerate(seq + [office_id]):
                sid = f"{rid}-S{si + 1}"
                stop_ids.append(sid)
                is_drop = si == len(seq)
                board = [] if is_drop else [e for e in riders if emp_pick[e] == loc]
                done = (status == "completed") or (status == "inProgress" and si == 0) or (
                    status == "delayed" and si < 2 and not is_drop)
                planned_at = depart + leg_min[si]
                stops.append(("route_stops", sid, dict(
                    routeId=rid, locationId=loc, sequence=si + 1, kind="drop" if is_drop else "pickup",
                    plannedArrival=ts(d, planned_at),
                    actualArrival=ts(d, planned_at + (6 if status == "delayed" else 1)) if done else None,
                    departedAt=ts(d, planned_at + 3) if done and not is_drop else None,
                    expectedBoardingCount=len(board), expectedAlightingCount=len(riders) if is_drop else 0,
                    boardingEmployeeIds=board, alightingEmployeeIds=riders if is_drop else [],
                    status="completed" if done else "upcoming")))
            boarded = {"completed": riders, "inProgress": [e for e in riders if emp_pick[e] == seq[0]],
                       "delayed": [e for e in riders if emp_pick[e] in seq[:2]]}.get(status, [])
            routes.append(("routes", rid, dict(
                routeCode=rid, name=f"{c['office'][0]} login route {n:02d}", employerId=code,
                estimatedDistanceKm=dist, estimatedDurationMinutes=dur, variant="ringRoad",
                status={"completed": "completed", "cancelled": "cancelled", "inProgress": "active",
                        "delayed": "active"}.get(status, "planned"))))
            t = dict(tripCode=tid, employerId=code, routeId=rid, serviceDate=ts(d), scheduledStart=ts(d, depart),
                     scheduledArrival=ts(d, arrive), originLocationId=seq[0], destinationLocationId=office_id,
                     distanceKm=dist, estimatedDurationMinutes=dur, employeeIds=riders,
                     absentEmployeeIds=[emp_ids[i] for i in absent], orderedStopIds=seq + [office_id],
                     instructions=NOTE, assignedDriverId=drv, assignedVehicleId=veh)
            final = dict(t)
            if status in ("completed", "inProgress", "delayed"):
                final.update(actualStart=ts(d, depart + 2), boardedPassengerIds=boarded,
                             startOdometerKm=r2(odo[di]))
            if status == "delayed":
                final["delayReason"] = "Heavy traffic on the ring road (simulated traffic feed)"
            if status == "completed":
                odo[di] += dist
                actual = dur + 3
                final.update(actualArrival=ts(d, depart + 2 + actual), actualDurationMinutes=actual,
                             alightedPassengerIds=riders, endOdometerKm=r2(odo[di]),
                             completionNotes="All passengers dropped at the office (demo).",
                             billableStatus="billed")
                completed_by_driver[drv] += 1
                rec_total = r2(dist * c["rate"] + c["extra"])
                bid = f"DEMO-{k}-BL{n:02d}"
                in_period = start_date <= d <= end_date
                billing.append(("billing_records", bid, dict(
                    employerId=code, tripId=tid, tripCode=tid, completedAt=final["actualArrival"],
                    billableDistanceKm=dist, ratePerKm=c["rate"], additionalCharges=c["extra"], adjustment=0,
                    totalAmount=rec_total, status="generated", createdAt=final["actualArrival"],
                    invoiceId=f"DEMO-{k}-INV1" if in_period else None,
                    calculationNotes=(f"{dist:.1f} km x INR {c['rate']:.2f}/km = INR {dist * c['rate']:.2f}"
                                      + (f" + INR {c['extra']:.2f} additional" if c["extra"] else "")
                                      + f" = INR {rec_total:.2f}"))))
                if in_period:
                    inv_records.append((bid, tid, dist, c["rate"], c["extra"], rec_total))
            if status == "cancelled":
                final["cancellationReason"] = "Office closed for scheduled maintenance (demo)"
            trips.append(dict(id=tid, target=status, base=t, final=final))
            # bookings of the riders
            bstat = {"completed": "completed", "cancelled": "cancelled", "planned": "approved"}.get(status,
                                                                                                    "assigned")
            for e in riders:
                bookings.append(("bookings", f"DEMO-{k}-B{len(bookings) + 1:02d}", dict(
                    reference=f"DEMO-{k}-B{len(bookings) + 1:02d}", employeeId=e, tripId=tid,
                    requestedPickupLocationId=emp_pick[e], requestedDestinationId=office_id,
                    requestedDate=ts(d), requestedArrivalMinutes=sh["startMinutes"], status=bstat,
                    decisionReason=final.get("cancellationReason") if status == "cancelled" else None,
                    notes=NOTE, createdAt=ts(d - dt.timedelta(days=1), 1080))))
            for i in absent:
                e = emp_ids[i]
                bookings.append(("bookings", f"DEMO-{k}-B{len(bookings) + 1:02d}", dict(
                    reference=f"DEMO-{k}-B{len(bookings) + 1:02d}", employeeId=e, tripId=None,
                    requestedPickupLocationId=emp_pick[e], requestedDestinationId=office_id,
                    requestedDate=ts(d), requestedArrivalMinutes=sh["startMinutes"], status="cancelled",
                    decisionReason="Not travelling (absence reported by the employee)", notes=NOTE,
                    createdAt=ts(today, 600))))
        day2 = today + dt.timedelta(days=2)
        for e_i, st, why in ((6, "requested", None), (7, "waitlisted", "All vehicles in the window are full"),
                             (8, "rejected", "Requested arrival is outside the service window")):
            e = emp_ids[e_i]
            bookings.append(("bookings", f"DEMO-{k}-B{len(bookings) + 1:02d}", dict(
                reference=f"DEMO-{k}-B{len(bookings) + 1:02d}", employeeId=e, tripId=None,
                requestedPickupLocationId=emp_pick[e], requestedDestinationId=office_id, requestedDate=ts(day2),
                requestedArrivalMinutes=540, status=st, decisionReason=why, notes=NOTE,
                createdAt=ts(today, 660))))
        if inv_records:
            invoices.append(("invoices", f"DEMO-{k}-INV1", dict(
                reference=f"INV-{code}-{period_code}", employerId=code, cycle=c["cycle"],
                periodStart=ts(start_date), periodEnd=ts(end_date), sequence=1,
                distanceKm=r2(sum(r[2] for r in inv_records)),
                distanceCharges=r2(sum(r[2] * r[3] for r in inv_records)),
                additionalCharges=r2(sum(r[4] for r in inv_records)), adjustments=0,
                total=r2(sum(r[5] for r in inv_records)), status="generated",
                generatedAt=ts(end_date + dt.timedelta(days=1), 360), tripIds=[r[1] for r in inv_records],
                notes=NOTE)))
        # fleet rows for this company
        active_today = {sp[2]: sp[0] for sp in specs if sp[1] == today and sp[2] is not None}
        for i, (vt, prop, fuel, seats, lux) in enumerate(VEHICLES):
            st = {"inProgress": "onTrip", "delayed": "onTrip", "assigned": "assigned"}.get(
                active_today.get(i, ""), "available")
            if i == 4:
                st = "maintenance"
            fleet_rows.append(("vehicles", vids[i], dict(
                vehicleCode=vids[i], fictionalRegistration=f"KA 00 DM {ci + 1}{i + 1:03d}", type=vt,
                propulsion=prop, fuelType=fuel, totalSeats=seats, luxuryClass=lux, status=st,
                odometerKm=r2(odo[i]), batteryOrFuelPercent=[72, 64, 88, 55, 30][i],
                assignedDriverId=dids[i] if i < 4 else None, assignedEmployerId=code,
                homeParkingId=PARKING_OF[k], model=["Demo Sedan LX", "Demo MPV Hybrid", "Demo EV Sedan",
                                                    "Demo Shuttle Van", "Demo EV Sedan"][i],
                color=["White", "Silver", "Blue", "White", "Grey"][i], active=True,
                lastInspectionAt=ts(today - dt.timedelta(days=i + 1), 360),
                notes=NOTE + (" - brake pads being replaced" if i == 4 else ""))))
        for i in range(5):
            st = {"inProgress": "onTrip", "delayed": "onTrip", "assigned": "assigned"}.get(
                active_today.get(i, ""), "available")
            if i == 4:
                st = "offDuty"
            name = next(dn)
            fleet_rows.append(("drivers", dids[i], dict(
                driverCode=dids[i], name=name, fictionalContact=f"+91 00000 9{ci + 1}{i + 1:02d}", status=st,
                assignedVehicleId=vids[i] if i < 4 else None, shiftStartMinutes=360, shiftEndMinutes=1080,
                attendanceStatus="checkedIn" if i < 3 else "notCheckedIn",
                safetyChecklistStatus="passed" if i < 2 else "pending",
                completedTripCount=completed_by_driver.get(dids[i], 0), licenceClass="LMV (demo)",
                licenceNumber=f"DEMO-DL-{k}-{i + 1:04d}", licenceExpiry=ts(dt.date(2029, 3, 31)),
                email=f"driver{i + 1}@{c['domain']}", address="Bengaluru (fictional address)",
                gender="male" if i % 2 == 0 else "notSpecified", active=True, notes=NOTE)))
        fleet_rows.append(("contracts", f"DEMO-CT-{k}", dict(
            contractCode=f"DEMO-CT-{k}", employerId=code, documentRef=f"DEMO-AGR-{k}-2026",
            validFrom=ts(dt.date(2026, 4, 1)), validUntil=ts(dt.date(2027, 3, 31)), status="active",
            agreedVehicleCount=4, costPerVehicle=42000 + 2500 * ci, currency="INR", costPeriod="month",
            hardVehicleLimit=ci == 1, contactName=f"Transport desk, {c['name']}", notes=NOTE)))
        for i in range(4):
            for rtype, ids in (("vehicle", vids), ("driver", dids)):
                fleet_rows.append(("fleet_allocations", f"DEMO-FA-{k}-{rtype[0].upper()}{i + 1}", dict(
                    employerId=code, resourceType=rtype, resourceId=ids[i], windowStartMinutes=c["window"][0],
                    windowEndMinutes=c["window"][1], label=f"{c['name']} day service", active=True)))
        fleet_rows.append(("optimization_configs", f"DEMO-OC-{k}", dict(
            employerId=code, name=f"{c['name']} profile", enabled=True, costWeight=c["cost"] / 10,
            punctualityWeight=c["punctual"] / 10, distanceWeight=4, travelTimeWeight=5, utilizationWeight=3,
            evPreferenceWeight=c["ev"] / 10, customerPreferenceWeight=3, safetyWeight=8,
            guardRequirementWeight=5, detourWeight=3, pickupProximityWeight=2, employeePreferenceWeight=3,
            maximumArrivalMinutes=570, minimumBufferMinutes=10, minimumEvPercentage=c["ev"] // 2,
            allowedVehicleTypes=["sedan", "mpv", "van"])))
        # fleet operations of this company's trips: duty shifts, inspection + breath test before driving
        ops = []
        duty = defaultdict(set)
        for sp in specs:
            if sp[2] is not None and sp[0] != "planned":
                duty[(sp[2], sp[1])].add(sp[0])
        for (di, d), sts in sorted(duty.items()):
            done = d < today
            ops.append(("shifts", f"DEMO-SH-{k}-D{di + 1}-{d:%Y%m%d}", dict(
                driverId=dids[di], shiftDate=ts(d), scheduledStart=ts(d, 360), scheduledEnd=ts(d, 1080),
                actualCheckIn=ts(d, 355), actualCheckOut=ts(d, 1085) if done else None,
                status="completed" if done else "checkedIn", notes=NOTE)))
        for n, sp in enumerate(specs, 1):
            if sp[1] == today and sp[0] in ("inProgress", "delayed"):
                di, tid = sp[2], f"DEMO-{k}-T{n:02d}"
                ops.append(("inspections", f"DEMO-IN-{k}-T{n:02d}", dict(
                    vehicleId=vids[di], driverId=dids[di], tripId=tid, date=ts(today, 340),
                    submittedAt=ts(today, 345), overallStatus="passed", notes=NOTE, items=[
                        dict(key=key, label=label, critical=crit, result="pass", note="")
                        for key, label, crit in (("tyres", "Tyres and wheels", True), ("brakes", "Brakes", True),
                                                 ("lights", "Lights and indicators", True),
                                                 ("seatbelts", "Seat belts", True),
                                                 ("firstAid", "First-aid kit", False))])))
                ops.append(("alcohol_tests", f"DEMO-AT-{k}-T{n:02d}", dict(
                    driverId=dids[di], vehicleId=vids[di], tripId=tid, testedAt=ts(today, 348), readingMg=0,
                    thresholdMg=0, result="pass", method="Breath test (simulated device)", note=NOTE)))
        plan.fleet_assets += fleet_rows
        plan.fleet_ops += ops
        delayed_n = next(n for n, sp in enumerate(specs, 1) if sp[0] == "delayed")
        d_delayed = specs[delayed_n - 1][2]
        extras = [
            ("incidents", f"DEMO-INC-{k}-1", dict(
                reference=f"DEMO-INC-{k}-1", type="delay", tripId=f"DEMO-{k}-T{delayed_n:02d}",
                driverId=dids[d_delayed], vehicleId=vids[d_delayed], severity="low", status="submittedDemo",
                reportedByRole="driver", reportedById=dids[d_delayed],
                description="Running about 15 minutes late because of heavy ring-road traffic (simulated).",
                createdAt=ts(today, 470))),
            ("notifications", f"DEMO-N-{k}-1", dict(
                recipientRole="employerAdmin", title="Invoice ready (demo)",
                body=f"Invoice INV-{code}-{period_code} for the closed {c['cycle']} period was generated.",
                category="billing", createdAt=ts(end_date + dt.timedelta(days=1), 361),
                relatedEntityType="invoice", relatedEntityId=f"DEMO-{k}-INV1", demoOnly=True)),
            ("notifications", f"DEMO-N-{k}-2", dict(
                recipientRole="employee", recipientId=emp_ids[3], title="Your cab is running late",
                body="Your pickup is delayed by about 15 minutes (simulated traffic).", category="delay",
                createdAt=ts(today, 472), relatedEntityType="trip",
                relatedEntityId=f"DEMO-{k}-T{delayed_n:02d}", demoOnly=True)),
            ("support_tickets", f"DEMO-TK-{k}-1", dict(
                reference=f"DEMO-TK-{k}-1", createdByRole="employee", createdById=emp_ids[0], category="trip",
                subject="Pickup point signage", relatedTripId=f"DEMO-{k}-T01", status="resolved",
                description="The pickup point board at the gate is hard to see in the morning.",
                response="Thanks - the pickup point description has been updated (demo).",
                createdAt=ts(past[0], 900), updatedAt=ts(past[0] + dt.timedelta(days=1), 600))),
        ]
        plan.companies.append(dict(
            key=k, code=code, slug=c["slug"], name=c["name"],
            registration=dict(slug=c["slug"], employer=dict(
                code=code, name=c["name"], status="active", currency="INR", maximumArrivalMinutes=570,
                defaultDistanceRate=c["rate"], preferredEvPercentage=c["ev"], luxuryPreference="standard",
                costPriority=c["cost"], punctualityPriority=c["punctual"], securityGuardPolicyEnabled=False,
                allowSharedPooling=True, pickupToleranceMinutes=7, billingCycle=c["cycle"], billingActive=True,
                additionalChargePerTrip=c["extra"], contactName="Transport Desk (fictional)",
                contactEmail=f"transport@{c['domain']}", address=f"{c['office'][0]}, Bengaluru (fictional)",
                vehicleTypePreferences=["sedan", "mpv"], shifts=SHIFTS),
                admin=dict(email=f"admin@{c['domain']}", displayName=f"{c['name']} Admin")),
            base=locs + emps + routes, stops=stops, trips=trips, bookings=bookings, billing=billing,
            invoices=invoices, extras=extras,
            employee_logins=[(emps[i][1], emps[i][2]["fictionalEmail"], emps[i][2]["name"]) for i in (0, 3)],
            driver_login=(dids[0], f"driver1@{c['domain']}", code),
            admin_email=f"admin@{c['domain']}"))
    plan.fleet_assets += [
        ("optimization_configs", "DEMO-OC-FLEET", dict(
            name="Fleet default profile (demo)", enabled=True, costWeight=5, punctualityWeight=6,
            distanceWeight=4, travelTimeWeight=5, utilizationWeight=4, evPreferenceWeight=5,
            customerPreferenceWeight=3, safetyWeight=8, guardRequirementWeight=5, detourWeight=3,
            pickupProximityWeight=2, employeePreferenceWeight=3, maximumArrivalMinutes=570,
            minimumBufferMinutes=10, minimumEvPercentage=30, allowedVehicleTypes=["sedan", "mpv", "van"])),
        ("geofences", "DEMO-GF-SERVICE", dict(
            name="Demo service area (simulated)", kind="serviceArea", centerX=15, centerY=18, radiusKm=22,
            active=True, notes=NOTE, vehicleIds=vehicle_ids_all)),
        ("geofences", "DEMO-GF-RESTRICTED", dict(
            name="Demo restricted zone (simulated)", kind="restrictedZone", centerX=4, centerY=30, radiusKm=1.5,
            active=True, notes=NOTE, vehicleIds=[])),
        # singletons: created only if the workspace has none yet (an existing policy is never changed)
        ("fleet_policy", "fleet", dict(
            alcoholMonitoringEnabled=True, alcoholThresholdMg=0, alcoholCheckPoint="shiftStart", speedLimitKmh=60,
            speedAlertsEnabled=True, geofenceAlertsEnabled=True, routeCorridorKm=1, autoMatchAdhoc=True,
            maxAddedDetourMinutes=10, sosRecipients=["fleetManager", "companyAdmin", "supportTeam"])),
        ("traffic_state", "current", dict(
            internalRoads="moderate", ringRoad="heavy", source="simulated",
            note="Simulated traffic (no live traffic feed is connected)")),
    ]
    return plan


# --------------------------------------------------------------------------- report
class Report:
    def __init__(self, out=print):
        self.rows: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.out = out

    def add(self, ws: str, resource: str, outcome: str, n: int = 1) -> None:
        if n:
            self.rows[(ws, resource)][outcome] += n

    def failed(self, ws: str, what: str, message: str, rows: list | None = None) -> None:
        self.problems.append(f"{ws}: {what}: {message}")
        for r in rows or []:
            self.add(ws, r[0], "failed")

    def total(self, outcome: str) -> int:
        return sum(v.get(outcome, 0) for v in self.rows.values())

    def print(self, dry_run: bool) -> None:
        created = "would create" if dry_run else "created"
        self.out(f"\n{'workspace':<18} {'resource':<22} {created:>12} {'existing':>9} {'skipped':>8} {'failed':>7}")
        for (ws, res), v in sorted(self.rows.items()):
            self.out(f"{ws:<18} {res:<22} {v.get('created', 0):>12} {v.get('existing', 0):>9} "
                     f"{v.get('skipped', 0):>8} {v.get('failed', 0):>7}")
        self.out(f"TOTAL {created}: {self.total('created')}, existing: {self.total('existing')}, "
                 f"skipped: {self.total('skipped')}, failed: {self.total('failed')}")
        for n in self.notes:
            self.out(f"note: {n}")
        for p in self.problems:
            self.out(f"PROBLEM: {p}")


# --------------------------------------------------------------------------- database reads (existence only)
def _schema_exists(cur, schema: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
    return cur.fetchone() is not None


def existing(cur, schema: str | None, resource: str, ids: list[str]) -> dict[str, dict]:
    """id -> {'status': ...} for the ids that already exist (read-only)."""
    from psycopg2 import sql
    from app.spec import ENTITIES
    if not schema or not ids or not _schema_exists(cur, schema):
        return {}
    e = ENTITIES["parking_locations" if resource == "locations" and schema == "fleet_workspace" else resource]
    has_status = e.col("status") is not None
    cur.execute(sql.SQL("SELECT id{} FROM {}.{} WHERE id = ANY(%s)").format(
        sql.SQL(", status") if has_status else sql.SQL(""), sql.Identifier(schema), sql.Identifier(e.table)),
        (list(ids),))
    return {r[0]: {"status": r[1] if has_status else None} for r in cur.fetchall()}


def company_schema(cur, code: str) -> str | None:
    if not _schema_exists(cur, "fleet_workspace"):
        return None
    cur.execute('SELECT t.schema_name FROM "fleet_workspace"."companies" c JOIN public.tenants t '
                "ON t.id = c.tenant_id WHERE c.code = %s AND t.status = 'ready'", (code,))
    r = cur.fetchone()
    return r[0] if r else None


# --------------------------------------------------------------------------- API client
class Api:
    def __init__(self, client):
        self.c = client
        self.h: dict[str, str] = {}

    def call(self, method: str, path: str, **kw):
        r = self.c.request(method, "/api/v1" + path, headers=kw.pop("headers", self.h), **kw)
        if r.status_code >= 300:
            try:
                msg = r.json().get("error", {}).get("message") or r.json().get("detail") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {msg}")
        return r.json()

    def signin(self, email: str, password: str, tenant: str, role: str) -> dict[str, str]:
        tok = self.call("POST", "/auth/signin", headers={}, json=dict(email=email, password=password,
                                                                    tenant=tenant, role=role))["accessToken"]
        return {"Authorization": f"Bearer {tok}"}

    def push(self, slug: str, rows: list, headers=None) -> None:
        if rows:
            self.call("POST", f"/t/{slug}/sync/push", headers=headers or self.h, json={"changes": [
                {"resource": r, "op": "put", "id": i, "data": d} for r, i, d in rows]})


# --------------------------------------------------------------------------- seeding steps
def ensure_fleet_admin(password: str, report: Report, dry_run: bool) -> None:
    """The demo Fleet Admin (same steps as scripts/create_fleet_manager.py, but never overwrites)."""
    from app import admins, db, security
    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT id::text FROM public.platform_users WHERE lower(email) = lower(%s)",
                    (FLEET_ADMIN_EMAIL,))
        if cur.fetchone():
            report.add("fleet", "account:fleetAdmin", "existing")
            return
        if dry_run:
            report.add("fleet", "account:fleetAdmin", "created")
            return
        cur.execute("SELECT id FROM public.tenants WHERE kind='fleet' AND status='ready'")
        fleet = cur.fetchone()
        h = security.hash_password(password)
        cur.execute("INSERT INTO public.platform_users(email, password_hash, display_name) VALUES (%s,%s,%s) "
                    "RETURNING id::text", (FLEET_ADMIN_EMAIL, h, "Demo Fleet Admin"))
        uid = cur.fetchone()["id"]
        cur.execute("INSERT INTO public.tenant_memberships(user_id, tenant_id, role) VALUES (%s,%s,'fleetManager')",
                    (uid, fleet["id"]))
        cur.execute('INSERT INTO "fleet_workspace"."fleet_users"(user_id, role) VALUES (%s,\'fleetManager\') '
                    "ON CONFLICT (user_id) DO NOTHING", (uid,))
        admins.ensure_admin(cur, uid)
        admins.set_password(cur, uid, h)
        admins.set_status(cur, uid, True)
        cur.execute(f"UPDATE {admins.ADMINS} SET name=%s, email=%s, updated_at=now() WHERE user_id=%s::uuid",
                    ("Demo Fleet Admin", FLEET_ADMIN_EMAIL, uid))
    report.add("fleet", "account:fleetAdmin", "created")


def _missing(cur, schema, rows, report: Report, ws: str) -> list:
    """Rows whose id does not exist yet; the others are counted as existing and left untouched."""
    by_res = defaultdict(list)
    for r in rows:
        by_res[r[0]].append(r[1])
    have = {res: existing(cur, schema, res, ids) for res, ids in by_res.items()}
    out = []
    for r in rows:
        if r[1] in have[r[0]]:
            report.add(ws, r[0], "existing")
        else:
            out.append(r)
    return out


def _push(api: Api, slug: str, rows: list, report: Report, ws: str, dry_run: bool, what: str,
          headers=None) -> bool:
    if not rows:
        return True
    if not dry_run:
        try:
            api.push(slug, rows, headers)
        except RuntimeError as exc:
            report.failed(ws, what, str(exc), rows)
            return False
    for r in rows:
        report.add(ws, r[0], "created")
    return True


TRIP_PATH = {"completed": ["assigned", "inProgress", "completed"], "delayed": ["assigned", "inProgress", "delayed"],
             "inProgress": ["assigned", "inProgress"], "assigned": ["assigned"], "planned": ["planned"],
             "cancelled": ["planned", "cancelled"]}


def _trip_stage(trip: dict, stage: str) -> dict:
    """The trip as it looks at one step of its normal life cycle."""
    if stage == trip["target"]:
        data = dict(trip["final"])
    else:
        data = dict(trip["base"])
        if stage == "inProgress":
            data.update({k: trip["final"][k] for k in ("actualStart", "boardedPassengerIds", "startOdometerKm")
                         if k in trip["final"]})
    if stage == "planned" and trip["target"] in ("planned", "cancelled"):
        data["assignedDriverId"] = data["assignedVehicleId"] = None
    data["status"] = stage
    return data


def seed(client, password: str, anchor: dt.date, dry_run: bool = False, out=print) -> Report:
    """Seed through the API of ``client`` (a TestClient of app.main.app on the target database)."""
    from app import db
    report = Report(out)
    plan = build_plan(anchor)
    api = Api(client)
    ensure_fleet_admin(password, report, dry_run)
    db_exists = True
    if not dry_run:
        api.h = api.signin(FLEET_ADMIN_EMAIL, password, "fleet", "fleetManager")

    def cur_ctx():
        return db.transaction()

    # 1. Fleet Manager (Fleet Admin -> Manager Management)
    with cur_ctx() as cur:
        cur.execute("SELECT 1 FROM public.platform_users WHERE lower(email) = lower(%s)", (FLEET_MANAGER_EMAIL,))
        has_mgr = cur.fetchone() is not None
    if has_mgr:
        report.add("fleet", "account:fleetManager", "existing")
    elif not dry_run:
        try:
            api.call("POST", "/t/fleet/staff/managers", json=dict(
                name="Demo Fleet Manager", email=FLEET_MANAGER_EMAIL, phone="+91 00000 80001", password=password))
            report.add("fleet", "account:fleetManager", "created")
        except RuntimeError as exc:
            report.failed("fleet", "Fleet Manager", str(exc))
    else:
        report.add("fleet", "account:fleetManager", "created")

    # 2. companies, through the company-registration workflow (one tenant schema each)
    schemas = {}
    for co in plan.companies:
        clash = None
        with cur_ctx() as cur:
            schemas[co["code"]] = company_schema(cur, co["code"])
            if _schema_exists(cur, "fleet_workspace"):          # a real company with the same name?
                cur.execute("SELECT code FROM fleet_workspace.companies WHERE lower(name) = lower(%s) "
                            "AND code <> %s", (co["name"], co["code"]))
                clash = cur.fetchone()
        if clash:
            report.failed(co["code"], "company", f"name '{co['name']}' is used by company {clash[0]}; not created")
            continue
        if schemas[co["code"]]:
            report.add(co["code"], "company (tenant)", "existing")
            continue
        if dry_run:
            report.add(co["code"], "company (tenant)", "created")
            schemas[co["code"]] = None
            continue
        try:
            reg = dict(co["registration"])
            reg["admin"] = dict(reg["admin"], password=password)
            res = api.call("POST", "/t/fleet/companies", json=reg)
            schemas[co["code"]] = res["tenant"]["schemaName"]
            report.add(co["code"], "company (tenant)", "created")
            report.add(co["code"], "account:employerAdmin", "created")
        except RuntimeError as exc:
            report.failed(co["code"], "company registration", str(exc))

    ready = [co for co in plan.companies if schemas.get(co["code"]) or dry_run]
    # 3. fleet assets (parking, vehicles, drivers, contracts, allocations, profiles, policy, geofences)
    with cur_ctx() as cur:
        rows = _missing(cur, "fleet_workspace", plan.fleet_assets, report, "fleet")
    wanted_codes = {co["code"] for co in ready}
    rows = [r for r in rows if r[2].get("employerId") in (None, *wanted_codes)
            and r[2].get("assignedEmployerId") in (None, *wanted_codes)]
    _push(api, "fleet", rows, report, "fleet", dry_run, "fleet assets")

    # 4. company data, trips through their normal life cycle, then billing / bookings / extras
    for co in ready:
        ws, slug, schema = co["code"], co["slug"], schemas.get(co["code"])
        if schema:
            slug = schema.removeprefix("tenant_")
        with cur_ctx() as cur:
            base = _missing(cur, schema, co["base"], report, ws)
            have_trips = existing(cur, schema, "trips", [t["id"] for t in co["trips"]])
        if not _push(api, slug, base, report, ws, dry_run, "locations / employees / routes"):
            continue
        stages: dict[int, list] = defaultdict(list)
        new_trips = 0
        for t in co["trips"]:
            path = TRIP_PATH[t["target"]]
            cur_status = have_trips.get(t["id"], {}).get("status")
            if cur_status is None:
                todo = path
                new_trips += 1
            elif cur_status == t["target"]:
                report.add(ws, "trips", "existing")
                continue
            elif cur_status in path:            # an earlier run stopped half-way: continue the same life cycle
                todo = path[path.index(cur_status) + 1:]
                report.notes.append(f"{ws}: trip {t['id']} continued from '{cur_status}' to '{t['target']}'")
                report.add(ws, "trips", "existing")
            else:                               # changed by someone since seeding: leave it alone
                report.add(ws, "trips", "skipped")
                report.notes.append(f"{ws}: trip {t['id']} is '{cur_status}' (changed since seeding); left as is")
                continue
            for i, stage in enumerate(todo):
                stages[i].append(("trips", t["id"], _trip_stage(t, stage)))
        ok = True
        if not dry_run:
            for i in sorted(stages):
                try:
                    api.push(slug, stages[i])
                except RuntimeError as exc:
                    report.failed(ws, f"trip step {i + 1}", str(exc))
                    report.add(ws, "trips", "failed", new_trips)
                    ok = False
                    break
        if not ok:
            continue
        report.add(ws, "trips", "created", new_trips)
        with cur_ctx() as cur:
            later = _missing(cur, schema, co["stops"] + co["invoices"] + co["billing"] + co["bookings"]
                             + co["extras"], report, ws)
        _push(api, slug, later, report, ws, dry_run, "stops / billing / invoices / bookings / incidents")

        # 5. logins: employees by the company's own Employer Admin, the first driver by the Fleet Admin
        if dry_run:
            with cur_ctx() as cur:
                for role, ids in (("employee", [e[0] for e in co["employee_logins"]]),
                                  ("driver", [co["driver_login"][0]])):
                    cur.execute("SELECT count(*) FROM public.tenant_memberships WHERE role = %s "
                                "AND scope_id = ANY(%s)", (role, ids))
                    have = cur.fetchone()[0]
                    report.add(ws, f"account:{role}", "existing", have)
                    report.add(ws, f"account:{role}", "created", len(ids) - have)
            continue
        try:
            ea = api.signin(co["admin_email"], password, slug, "employerAdmin")
            for eid, email, name in co["employee_logins"]:
                if api.call("GET", f"/t/{slug}/employees/{eid}/login", headers=ea)["hasLogin"]:
                    report.add(ws, "account:employee", "existing")
                    continue
                api.call("PUT", f"/t/{slug}/employees/{eid}/login", headers=ea,
                         json=dict(email=email, password=password, displayName=name))
                report.add(ws, "account:employee", "created")
        except RuntimeError as exc:
            report.failed(ws, "employee logins", str(exc))
        did, email, code = co["driver_login"]
        try:
            if api.call("GET", f"/t/fleet/drivers/{did}/login")["assignments"]:
                report.add(ws, "account:driver", "existing")
            else:
                api.call("PUT", f"/t/fleet/drivers/{did}/login", json=dict(email=email, password=password,
                                                                         company=code))
                report.add(ws, "account:driver", "created")
        except RuntimeError as exc:
            report.failed(ws, "driver login", str(exc))

    # 6. fleet operations that refer to the trips (duty shifts, inspections, breath tests)
    with cur_ctx() as cur:
        ops = _missing(cur, "fleet_workspace", plan.fleet_ops, report, "fleet")
    ops = [r for r in ops if r[1].split("-")[2] in {co["key"] for co in ready}]
    _push(api, "fleet", ops, report, "fleet", dry_run, "fleet operations")

    # 7. one audit event per workspace recording the seed run (only when something was created)
    if not dry_run and report.total("created"):
        stamp = f"{anchor:%Y%m%d}"
        for ws, slug in [("fleet", "fleet")] + [(co["code"], (schemas.get(co["code"]) or "").removeprefix(
                "tenant_")) for co in ready]:
            if not slug:
                continue
            made = sum(v.get("created", 0) for (w, _), v in report.rows.items() if w == ws)
            if not made:
                continue
            aid = f"DEMO-AU-SEED-{ws.replace('DEMO-', '')}-{stamp}"
            with cur_ctx() as cur:
                sch = "fleet_workspace" if ws == "fleet" else schemas[ws]
                if existing(cur, sch, "audit_events", [aid]):
                    continue
            _push(api, slug, [("audit_events", aid, dict(
                actorRole="fleetManager", actorId=FLEET_ADMIN_EMAIL, action="demo.seed", entityType="dataset",
                entityId=MARK, summary=f"Demo seed {MARK}: {made} synthetic record(s) created in this workspace",
                outcome="success", related={"demoSeed": MARK, "anchorDate": anchor.isoformat()},
                simulated=True))], report, ws, dry_run, "audit event")
    return report


# --------------------------------------------------------------------------- read-only verification
def verify(out=print) -> bool:
    """Count the demo records and check that they are consistent (reads only)."""
    from psycopg2 import sql
    from app import db, rules
    ok_all = True

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        out(f"[{'OK' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")

    with db.transaction(dict_rows=True) as cur:
        cur.execute("SELECT current_database() AS d")
        out(f"Database: {cur.fetchone()['d']}")
        cur.execute('SELECT c.code, c.name, t.slug, t.schema_name, t.status, t.schema_version '
                    'FROM fleet_workspace.companies c JOIN public.tenants t ON t.id = c.tenant_id '
                    "WHERE c.code LIKE 'DEMO-%%' ORDER BY c.code")
        companies = [dict(r) for r in cur.fetchall()]
        check("three demo companies registered", len(companies) == 3,
              ", ".join(f"{c['code']} -> {c['schema_name']} ({c['status']}, v{c['schema_version']})"
                        for c in companies))
        for sch in ["fleet_workspace"] + [c["schema_name"] for c in companies]:
            cur.execute("SELECT table_name FROM information_schema.columns WHERE table_schema = %s "
                        "AND column_name = 'id' AND data_type = 'text' ORDER BY 1", (sch,))
            counts = {}
            for t in [r["table_name"] for r in cur.fetchall()]:
                cur.execute(sql.SQL("SELECT count(*) AS n FROM {}.{} WHERE id LIKE 'DEMO-%%'").format(
                    sql.Identifier(sch), sql.Identifier(t)))
                n = cur.fetchone()["n"]
                if n:
                    counts[t] = n
            out(f"  {sch}: {sum(counts.values())} demo rows " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        cur.execute("SELECT count(*) AS n FROM public.platform_users WHERE email LIKE %s", (f"%@%{DOMAIN}",))
        users = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM public.platform_users WHERE email LIKE %s "
                    "AND password_hash NOT LIKE 'pbkdf2_sha256$%%'", (f"%@%{DOMAIN}",))
        check("demo accounts store only PBKDF2 hashes", cur.fetchone()["n"] == 0, f"{users} demo accounts")
        # fleet rules on the final data
        cur.execute("SELECT l.id, l.capacity, count(v.id) AS n FROM fleet_workspace.locations l LEFT JOIN "
                    "fleet_workspace.vehicles v ON v.home_parking_id = l.id WHERE l.id LIKE 'DEMO-%%' "
                    "GROUP BY l.id, l.capacity")
        park = cur.fetchall()
        check("parking capacity respected", all(r["n"] <= r["capacity"] for r in park),
              ", ".join(f"{r['id']} {r['n']}/{r['capacity']}" for r in park))
        cur.execute("SELECT a.id, a.resource_id, a.window_start_minutes s1, a.window_end_minutes e1, b.id bid, "
                    "b.window_start_minutes s2, b.window_end_minutes e2 FROM fleet_workspace.fleet_allocations a "
                    "JOIN fleet_workspace.fleet_allocations b ON a.resource_type = b.resource_type AND "
                    "a.resource_id = b.resource_id AND a.employer_id <> b.employer_id AND a.active AND b.active "
                    "AND a.id < b.id WHERE a.id LIKE 'DEMO-%%' OR b.id LIKE 'DEMO-%%'")
        clashes = [r for r in cur.fetchall() if rules.windows_overlap((r["s1"], r["e1"]), (r["s2"], r["e2"]))]
        check("no overlapping company allocations", not clashes, f"{len(clashes)} overlap(s)")
        cur.execute("SELECT count(DISTINCT resource_id) AS n, count(*) AS t FROM fleet_workspace.fleet_allocations "
                    "WHERE id LIKE 'DEMO-%%'")
        r = cur.fetchone()
        check("each demo vehicle / driver allocated to one company only", r["n"] == r["t"], f"{r['t']} allocations")
        for c in companies:
            s = sql.Identifier(c["schema_name"])
            q = lambda text, *a: (cur.execute(sql.SQL(text).format(s=s), a), cur.fetchall())[1]  # noqa: E731
            rows = q("SELECT count(*) n, count(DISTINCT id) d FROM {s}.employers")
            check(f"{c['code']}: schema holds only its own company", rows[0]["n"] == 1 and q(
                "SELECT id FROM {s}.employers")[0]["id"] == c["code"])
            bad = q("SELECT id, status FROM {s}.trips WHERE (status IN ('assigned','driverAccepted','ready',"
                    "'inProgress') AND (assigned_driver_id IS NULL OR assigned_vehicle_id IS NULL)) OR "
                    "(status = 'cancelled' AND coalesce(trim(cancellation_reason), '') = '') OR "
                    "(status = 'completed' AND (actual_arrival IS NULL OR distance_km IS NULL))")
            check(f"{c['code']}: trip statuses consistent", not bad, f"{len(bad)} invalid")
            paths = q("SELECT h.trip_id, t.status, string_agg(h.to_status, '>' ORDER BY h.id) p FROM "
                      "{s}.trip_status_history h JOIN {s}.trips t ON t.id = h.trip_id WHERE t.id LIKE 'DEMO-%%' "
                      "GROUP BY h.trip_id, t.status")
            legal = all(all(rules.can_transition(a, b) for a, b in zip(p["p"].split(">"), p["p"].split(">")[1:]))
                        for p in paths)
            check(f"{c['code']}: every trip followed the state machine", legal and len(paths) > 0,
                  f"{len(paths)} trips, statuses " + ", ".join(sorted({p['status'] for p in paths})))
            over = q("SELECT t.id FROM {s}.trips t JOIN fleet_workspace.vehicles v ON v.id = t.assigned_vehicle_id "
                     "WHERE (SELECT count(*) FROM {s}.trip_passengers p WHERE p.trip_id = t.id AND p.state = "
                     "'assigned') > v.total_seats - 1")
            check(f"{c['code']}: passengers fit the vehicle seats", not over, f"{len(over)} over capacity")
            dbl = q("SELECT a.id FROM {s}.trips a JOIN {s}.trips b ON a.assigned_driver_id = b.assigned_driver_id "
                    "AND a.id < b.id AND a.status <> 'cancelled' AND b.status <> 'cancelled' AND "
                    "a.scheduled_start < b.scheduled_arrival AND b.scheduled_start < a.scheduled_arrival")
            check(f"{c['code']}: no driver double-booked", not dbl)
            unbilled = q("SELECT t.id FROM {s}.trips t WHERE t.status = 'completed' AND NOT EXISTS (SELECT 1 FROM "
                         "{s}.billing_records b WHERE b.trip_id = t.id)")
            lines = q("SELECT id, billable_distance_km d, rate_per_km r, additional_charges a, adjustment j, "
                      "total_amount t FROM {s}.billing_records")
            wrong = [x for x in lines if round(float(x["d"]) * float(x["r"]) + float(x["a"]) + float(x["j"]), 2)
                     != float(x["t"])]
            check(f"{c['code']}: billing = distance x rate + charges, once per completed trip",
                  not unbilled and not wrong, f"{len(lines)} lines")
            inv = q("SELECT i.reference, i.total, i.distance_km, coalesce(sum(b.total_amount), 0) s, "
                    "coalesce(sum(b.billable_distance_km), 0) d, count(b.id) n FROM {s}.invoices i LEFT JOIN "
                    "{s}.billing_records b ON b.invoice_id = i.id GROUP BY i.id")
            check(f"{c['code']}: invoice totals equal their persisted lines",
                  all(round(float(x["total"]), 2) == round(float(x["s"]), 2) and
                      round(float(x["distance_km"]), 2) == round(float(x["d"]), 2) for x in inv),
                  "; ".join(f"{x['reference']} {float(x['total']):.2f} ({x['n']} lines)" for x in inv))
            cur.execute("SELECT count(*) AS n FROM public.tenant_memberships m JOIN public.tenants t ON "
                        "t.id = m.tenant_id WHERE t.schema_name = %s AND m.active", (c["schema_name"],))
            out(f"  {c['code']}: {cur.fetchone()['n']} active memberships (admin, employees, driver, fleet staff)")
    return ok_all


# --------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Seed the three demo companies and Fleet Workspace demo data.")
    ap.add_argument("--database", help="target database (default: DB_NAME from backend/.env)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be created; write nothing")
    ap.add_argument("--yes-demo-data", action="store_true", help="required to write (or ROUTEMATIC_DEMO_SEED=1)")
    ap.add_argument("--confirm-database", help="exact name of a non-demo database the owner approved")
    ap.add_argument("--init", action="store_true",
                    help="create the demo database / shared and fleet schemas first (demo databases only)")
    ap.add_argument("--verify", action="store_true", help="only check the demo data (counts, consistency)")
    ap.add_argument("--utc-offset", default="+05:30",
                    help="business timezone of the demo companies as an offset (default +05:30, India)")
    ap.add_argument("--anchor-date", type=dt.date.fromisoformat, default=dt.date.today(),
                    help="'today' for the demo timeline (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    global TZ
    sign, hh, mm = a.utc_offset[0], int(a.utc_offset[1:3]), int(a.utc_offset[4:6])
    TZ = dt.timezone((-1 if sign == "-" else 1) * dt.timedelta(hours=hh, minutes=mm))
    from app import config
    name = a.database or config.get_settings().db_name
    try:                                         # checked BEFORE the configuration is pointed at it
        kind = check_target(name, a.confirm_database, config.PROTECTED_DATABASES)
    except SeedRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if a.database:
        os.environ["DB_NAME"] = a.database
        config.get_settings.cache_clear()
    if a.verify:
        from app import db
        with db.transaction() as cur:
            db.assert_safe_target(cur)
        return 0 if verify() else 1
    if not a.dry_run and not (a.yes_demo_data or os.environ.get("ROUTEMATIC_DEMO_SEED") == "1"):
        print("REFUSED: writing demo data needs --yes-demo-data (or ROUTEMATIC_DEMO_SEED=1). "
              "Use --dry-run to preview.", file=sys.stderr)
        return 2
    password = os.environ.get("DEMO_SEED_PASSWORD", "")
    if not a.dry_run and len(password) < 8:
        print("REFUSED: set DEMO_SEED_PASSWORD (at least 8 characters) for the demo logins.", file=sys.stderr)
        return 2
    if a.init and kind != "demo":
        print("REFUSED: --init is only for development/demo databases.", file=sys.stderr)
        return 2
    from app import db, migrations, tenants
    print(f"Target database: {name} ({'development/demo database' if kind == 'demo' else 'owner-confirmed'})")
    if a.init and not a.dry_run:
        print(f"Database {'created' if db.ensure_database() else 'already exists'}")
        with db.transaction() as cur:
            migrations.apply_shared(cur)
        tenants.provision_fleet()
        tenants.upgrade_all_tenants()
    try:
        with db.transaction() as cur:
            db.assert_safe_target(cur)           # the live connection really is the checked database
    except Exception as exc:                     # noqa: BLE001 - report and stop
        if a.dry_run:
            print(f"Database not reachable or not initialised ({exc.__class__.__name__}); nothing exists yet.")
            report = Report()
            for co in build_plan(a.anchor_date).companies:
                report.add(co["code"], "company (tenant)", "created")
            report.print(True)
            return 0
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    from fastapi.testclient import TestClient
    from app.main import app
    print(f"Mode: {'DRY RUN (nothing is written)' if a.dry_run else 'WRITE'}; demo timeline anchored on "
          f"{a.anchor_date}; marker {MARK}")
    with TestClient(app) as client:
        report = seed(client, password, a.anchor_date, a.dry_run)
    report.print(a.dry_run)
    if a.dry_run:
        return 0
    print("\nVerification:")
    return 1 if (report.problems or not verify()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
