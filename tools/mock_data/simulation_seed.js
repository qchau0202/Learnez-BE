#!/usr/bin/env node
/**
 * Learnez simulation seed: ~1000 students + lecturers, persona-based behavior, batched Mongo/API writes.
 *
 * Reads learnez-supabase-db.csv (JSON-in-CSV dump) for courses, departments, faculties — no module_materials
 * are required for interaction (page_view / course-level events only).
 *
 * Usage:
 *   cd BE/tools/mock_data && npm install
 *   export MONGO_URI=mongodb://localhost:27017
 *   export MONGODB_RAW_DB=elearning_raw
 *   node simulation_seed.js --users 1000 --since-weeks 16 --batch 400 --mode mongo
 *
 * API mode (clock-warp: server sets created_at = event_time when X-Clock-Warp: 1):
 *   export ML_SIMULATION_SECRET=your-secret
 *   node simulation_seed.js --mode api --api-url http://127.0.0.1:8000/api/activity/sim/ingest-batch --api-secret "$ML_SIMULATION_SECRET"
 */

const { faker } = require('@faker-js/faker');
const { MongoClient } = require('mongodb');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

require('dotenv').config({ path: path.resolve(__dirname, '../../.env') });
require('dotenv').config({ path: path.resolve(__dirname, '../../../.env') });

const WEEKDAY = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

const STUDENT_PERSONAS = ['star', 'steady', 'uneven', 'struggling', 'sparse', 'dormant'];
const PERSONA_WEIGHTS = [0.15, 0.35, 0.2, 0.15, 0.1, 0.05];

function parseArgs() {
  const args = process.argv.slice(2);
  const out = {};
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a.startsWith('--')) {
      const key = a.replace(/^--/, '');
      const next = args[i + 1];
      if (!next || next.startsWith('--')) {
        out[key] = true;
      } else {
        out[key] = next;
        i++;
      }
    }
  }
  return out;
}

const argv = parseArgs();
const NUM_USERS = Math.max(1, Number(argv.users || 1000));
const SINCE_WEEKS = Math.max(1, Number(argv['since-weeks'] || 16));
const BATCH_SIZE = Math.max(50, Number(argv.batch || 400));
const MODE = (argv.mode || 'mongo').toLowerCase();
const API_URL = argv['api-url'] || process.env.SIM_API_URL || null;
const API_SECRET = argv['api-secret'] || process.env.ML_SIMULATION_SECRET || process.env.SIM_API_TOKEN || '';
const CSV_PATH =
  argv.csv ||
  process.env.SEED_CSV_PATH ||
  path.resolve(__dirname, '../../../learnez-supabase-db.csv');

const MONGO_URI = process.env.MONGO_URI || process.env.MONGODB_URI || 'mongodb://localhost:27017';
const RAW_DB = process.env.MONGODB_RAW_DB || process.env.MONGO_RAW_DB || 'elearning_raw';

const LECTURER_FRACTION = Number(argv['lecturer-fraction'] || 0.04);

function assertValidMongoUri(uri) {
  const u = (uri || '').trim();
  if (!u || u === '...') {
    throw new Error(
      'Set MONGO_URI to a real connection string (must start with mongodb:// or mongodb+srv://). ' +
        'The placeholder "..." or an empty value is invalid.',
    );
  }
  if (!/^mongodb(\+srv)?:\/\//i.test(u)) {
    throw new Error(
      `MONGO_URI must start with mongodb:// or mongodb+srv:// (got: "${u.slice(0, 24)}..."). ` +
        'Example: mongodb://localhost:27017 or mongodb+srv://user:pass@cluster.example.net/',
    );
  }
}

function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function randBetween(a, b) {
  return a + Math.random() * (b - a);
}

function randDateBetween(start, end) {
  return new Date(start.getTime() + Math.random() * (end.getTime() - start.getTime()));
}

function idem(prefix, ...parts) {
  return [prefix, ...parts.filter((p) => p != null && p !== '')].join('::');
}

/**
 * Export format: line 1 = `complete_learnez_dump`, line 2 = quoted JSON with `""` escapes.
 * Alternate: single line `complete_learnez_dump,"{""roles""...}"`
 */
function parseLearnezDumpCsv(content) {
  const trimmed = content.trim();
  const lines = trimmed.split('\n').map((l) => l.trim()).filter(Boolean);
  let quoted = null;
  if (lines.length >= 2 && lines[0] === 'complete_learnez_dump') {
    quoted = lines[1];
  } else {
    const prefix = 'complete_learnez_dump,';
    if (trimmed.startsWith(prefix)) {
      quoted = trimmed.slice(prefix.length);
    }
  }
  if (!quoted || !quoted.startsWith('"') || !quoted.endsWith('"')) {
    throw new Error(
      'CSV must be complete_learnez_dump on line 1 and quoted JSON on line 2 (or legacy single-line CSV)',
    );
  }
  const inner = quoted.slice(1, -1).replace(/""/g, '"');
  return JSON.parse(inner);
}

function loadSeed() {
  if (!fs.existsSync(CSV_PATH)) {
    console.warn(`CSV not found at ${CSV_PATH}; using minimal synthetic courses`);
    return {
      courses: Array.from({ length: 12 }, (_, i) => ({
        id: i + 1,
        title: `Demo Course ${i + 1}`,
        course_code: `DEMO${100 + i}`,
        from_department: (i % 4) + 1,
        course_start_date: '2026-05-01',
        course_end_date: '2026-08-30',
        course_session_date: WEEKDAY[1 + (i % 5)],
        course_occurences: 12,
        lecturer_id: faker.string.uuid(),
      })),
      departments: [
        { id: 1, name: 'Software Engineering', from_faculty: 1 },
        { id: 2, name: 'Computer Science', from_faculty: 1 },
        { id: 3, name: 'Business Administration', from_faculty: 2 },
        { id: 4, name: 'Public Finance', from_faculty: 3 },
      ],
      faculties: [
        { id: 1, name: 'Information Technology', faculty_code: 'IT' },
        { id: 2, name: 'Business Administration', faculty_code: 'BA' },
        { id: 3, name: 'Banking & Finance', faculty_code: 'BF' },
      ],
    };
  }
  const raw = fs.readFileSync(CSV_PATH, 'utf8');
  try {
    return parseLearnezDumpCsv(raw);
  } catch (e) {
    console.error('Failed to parse CSV dump:', e.message);
    throw e;
  }
}

function facultyLetter(facultyId) {
  if (facultyId === 2) return 'b';
  if (facultyId === 3) return 'f';
  return 'k';
}

function pickPersona() {
  const r = Math.random();
  let c = 0;
  for (let i = 0; i < PERSONA_WEIGHTS.length; i++) {
    c += PERSONA_WEIGHTS[i];
    if (r <= c) return STUDENT_PERSONAS[i];
  }
  return 'steady';
}

function nextWeekdayOnOrAfter(d, weekdayName) {
  const target = WEEKDAY.indexOf(weekdayName);
  if (target < 0) return new Date(d);
  const x = new Date(d);
  while (x.getDay() !== target) {
    x.setDate(x.getDate() + 1);
  }
  return x;
}

function sessionDatesForCourse(course, cap) {
  const start = new Date(course.course_start_date);
  const end = new Date(course.course_end_date);
  const maxN = Math.min(cap, course.course_occurences || 15);
  let cur = nextWeekdayOnOrAfter(start, course.course_session_date || 'Monday');
  const out = [];
  while (cur <= end && out.length < maxN) {
    out.push(new Date(cur));
    cur = new Date(cur);
    cur.setDate(cur.getDate() + 7);
  }
  return out;
}

function coursesForDepartment(deptId, allCourses) {
  return allCourses.filter((c) => Number(c.from_department) === Number(deptId));
}

function timingLabel(submittedAt, dueAt) {
  if (!dueAt) return 'on_time';
  const ms = submittedAt.getTime() - dueAt.getTime();
  const early = -36 * 3600 * 1000;
  if (ms < early) return 'early';
  if (ms <= 0) return 'on_time';
  return 'late';
}

function submissionTimeForPersona(persona, dueAt) {
  const due = dueAt.getTime();
  const h = 3600000;
  switch (persona) {
    case 'star':
      return new Date(due - randBetween(48, 120) * h);
    case 'steady':
      return new Date(due - randBetween(2, 20) * h);
    case 'uneven':
      return Math.random() < 0.55
        ? new Date(due - randBetween(1, 10) * h)
        : new Date(due + randBetween(4, 72) * h);
    case 'struggling':
      return Math.random() < 0.35
        ? new Date(due - randBetween(0.5, 6) * h)
        : new Date(due + randBetween(8, 96) * h);
    case 'sparse':
      return Math.random() < 0.5
        ? new Date(due - randBetween(1, 4) * h)
        : new Date(due + randBetween(12, 120) * h);
    case 'dormant':
      return new Date(due + randBetween(72, 200) * h);
    default:
      return new Date(due - randBetween(2, 12) * h);
  }
}

function attendanceRoll(persona) {
  const r = Math.random();
  switch (persona) {
    case 'star':
      if (r < 0.92) return 'Present';
      if (r < 0.97) return 'Late';
      return 'Absent';
    case 'steady':
      if (r < 0.88) return 'Present';
      if (r < 0.95) return 'Late';
      return 'Absent';
    case 'uneven':
      if (r < 0.65) return 'Present';
      if (r < 0.82) return 'Late';
      return 'Absent';
    case 'struggling':
      if (r < 0.45) return 'Present';
      if (r < 0.7) return 'Late';
      return 'Absent';
    case 'sparse':
      if (r < 0.35) return 'Present';
      if (r < 0.55) return 'Late';
      return 'Absent';
    case 'dormant':
      if (r < 0.12) return 'Present';
      if (r < 0.2) return 'Late';
      return 'Absent';
    default:
      return 'Present';
  }
}

function loginSessionsCount(persona) {
  switch (persona) {
    case 'star':
      return randInt(4, 7) * SINCE_WEEKS;
    case 'steady':
      return randInt(2, 4) * SINCE_WEEKS;
    case 'uneven':
      return randInt(1, 3) * SINCE_WEEKS;
    case 'struggling':
      return randInt(1, 2) * SINCE_WEEKS;
    case 'sparse':
      return randInt(0, 1) * SINCE_WEEKS + randInt(1, 4);
    case 'dormant':
      return randInt(0, 2);
    default:
      return SINCE_WEEKS * 2;
  }
}

function pageViewsPerCourse(persona) {
  switch (persona) {
    case 'star':
      return randInt(8, 20);
    case 'steady':
      return randInt(4, 10);
    case 'uneven':
      return randInt(2, 8);
    case 'struggling':
      return randInt(1, 5);
    case 'sparse':
      return randInt(0, 3);
    case 'dormant':
      return randInt(0, 1);
    default:
      return 4;
  }
}

function fullName() {
  return faker.person?.fullName?.() ?? faker.name.fullName();
}

function buildUserRecord(seed, index, roleId, persona, dept) {
  const uid = faker.string.uuid();
  const email = `sim.${roleId === 2 ? 'lec' : 'stu'}.${index}.${uid.slice(0, 8)}@sim.learnez.local`.toLowerCase();
  const facultyId = dept.from_faculty;
  const letter = facultyLetter(facultyId);
  const windowStart = new Date(Date.now() - SINCE_WEEKS * 7 * 24 * 3600 * 1000);
  const windowEnd = new Date();
  const created_at = randDateBetween(windowStart, windowEnd);

  if (roleId === 2) {
    const lid = `Lecturer${letter === 'k' ? 'K' : letter === 'b' ? 'B' : 'F'}${String(randInt(1000, 9999))}`;
    return {
      user_id: uid,
      email,
      role_id: 2,
      persona: 'lecturer',
      full_name: fullName(),
      is_active: true,
      created_at,
      lecturer_id: lid,
      faculty_id: facultyId,
      department_id: dept.id,
      department_name: dept.name,
    };
  }

  const mid = faker.helpers.arrayElement(['3', '5']);
  const studentId = `52${mid}${letter}${String(randInt(1000, 9999)).padStart(4, '0')}`;
  const cohortClass = `25${letter}50201`;
  return {
    user_id: uid,
    email,
    role_id: 3,
    persona,
    full_name: fullName(),
    is_active: true,
    created_at,
    student_id: studentId,
    faculty_id: facultyId,
    department_id: dept.id,
    department_name: dept.name,
    major: dept.name,
    student_class: cohortClass,
    enrolled_year: 2025,
    current_gpa: persona === 'star' ? randBetween(8, 9.5) : persona === 'struggling' ? randBetween(5, 6.8) : randBetween(6.5, 8.2),
    cumulative_gpa: persona === 'star' ? randBetween(8.2, 9.5) : randBetween(5.5, 8.5),
    gender: faker.helpers.arrayElement(['male', 'female']),
    phone_number: '09' + faker.string.numeric(8),
    date_of_birth: faker.date
      .between({ from: '2000-01-01', to: '2006-12-31' })
      .toISOString()
      .slice(0, 10),
  };
}

function simUserDoc(u) {
  const base = {
    idempotency_key: idem('simuser', u.user_id),
    user_id: u.user_id,
    email: u.email,
    role_id: u.role_id,
    persona: u.persona,
    full_name: u.full_name,
    faculty_id: u.faculty_id,
    department_id: u.department_id,
    created_at: u.created_at.toISOString(),
    simulated_from: u.created_at.toISOString(),
    schema_version: 1,
  };
  if (u.role_id === 3) {
    base.student_id = u.student_id;
    base.student_class = u.student_class;
    base.major = u.major;
    base.current_gpa = u.current_gpa;
  } else {
    base.lecturer_id = u.lecturer_id;
  }
  return base;
}

function eventShell(eventTime, userId, idKey, extra = {}) {
  const et = eventTime instanceof Date ? eventTime : new Date(eventTime);
  return {
    event_time: et,
    source: 'api',
    schema_version: 1,
    created_at: et,
    user_id: userId,
    idempotency_key: idKey,
    ...extra,
  };
}

async function bulkUpsertMongo(db, collection, docs) {
  if (!docs.length) return 0;
  const col = db.collection(collection);
  const ops = docs.map((d) => ({
    replaceOne: {
      filter: { idempotency_key: d.idempotency_key },
      replacement: d,
      upsert: true,
    },
  }));
  await col.bulkWrite(ops, { ordered: false });
  return docs.length;
}

async function postApiBatch(url, secret, collection, documents) {
  if (!documents.length) return 0;
  await axios.post(
    url,
    { collection, documents },
    {
      headers: {
        'Content-Type': 'application/json',
        'X-Simulation-Secret': secret,
        'X-Clock-Warp': '1',
      },
      timeout: 180000,
      maxBodyLength: Infinity,
      maxContentLength: Infinity,
    },
  );
  return documents.length;
}

async function flushBuffers(mongoDb, buffers, stats) {
  const tasks = [];
  for (const [coll, arr] of Object.entries(buffers)) {
    if (!arr.length) continue;
    const chunk = arr.splice(0, arr.length);
    if (MODE === 'mongo' && mongoDb) {
      tasks.push(
        bulkUpsertMongo(mongoDb, coll, chunk).then((n) => {
          stats.written += n;
        }),
      );
    } else if (MODE === 'api' && API_URL && API_SECRET) {
      tasks.push(
        postApiBatch(API_URL, API_SECRET, coll, chunk).then((n) => {
          stats.written += n;
        }),
      );
    }
  }
  await Promise.all(tasks);
}

async function main() {
  console.log(
    JSON.stringify(
      {
        users: NUM_USERS,
        sinceWeeks: SINCE_WEEKS,
        batch: BATCH_SIZE,
        mode: MODE,
        csv: CSV_PATH,
        api: MODE === 'api' ? API_URL : null,
      },
      null,
      2,
    ),
  );

  const seed = loadSeed();
  const departments = seed.departments || [];
  const courses = seed.courses || [];
  if (!departments.length || !courses.length) {
    throw new Error('Seed must include departments and courses');
  }

  const numLecturers = Math.min(Math.round(NUM_USERS * LECTURER_FRACTION), NUM_USERS - 1);
  const numStudents = NUM_USERS - numLecturers;

  const buffers = {
    activity_events: [],
    assessment_events: [],
    attendance_events: [],
    simulation_users: [],
  };
  const stats = { written: 0 };

  let mongoClient = null;
  let mongoDb = null;
  if (MODE === 'mongo') {
    assertValidMongoUri(MONGO_URI);
    mongoClient = new MongoClient(MONGO_URI);
    await mongoClient.connect();
    mongoDb = mongoClient.db(RAW_DB);
    console.log(`Mongo connected, db=${RAW_DB}`);
  } else if (MODE === 'api') {
    if (!API_URL || !API_SECRET) {
      throw new Error('API mode requires --api-url and --api-secret (or ML_SIMULATION_SECRET)');
    }
  } else {
    throw new Error('--mode must be mongo or api');
  }

  const windowStart = new Date(Date.now() - SINCE_WEEKS * 7 * 24 * 3600 * 1000);
  const windowEnd = new Date();

  async function enqueue(coll, doc) {
    buffers[coll].push(doc);
    if (buffers[coll].length >= BATCH_SIZE) {
      const chunk = buffers[coll].splice(0, BATCH_SIZE);
      if (MODE === 'mongo' && mongoDb) {
        stats.written += await bulkUpsertMongo(mongoDb, coll, chunk);
      } else {
        stats.written += await postApiBatch(API_URL, API_SECRET, coll, chunk);
      }
    }
  }

  const users = [];
  for (let i = 0; i < numLecturers; i++) {
    const dept = faker.helpers.arrayElement(departments);
    users.push(buildUserRecord(seed, i, 2, 'lecturer', dept));
  }
  for (let i = 0; i < numStudents; i++) {
    const dept = faker.helpers.arrayElement(departments);
    users.push(buildUserRecord(seed, numLecturers + i, 3, pickPersona(), dept));
  }

  for (const u of users) {
    await enqueue('simulation_users', simUserDoc(u));
  }

  let assignmentSeq = 300000;

  for (const u of users) {
    if (u.role_id === 2) {
      const deptCourses = coursesForDepartment(u.department_id, courses);
      const myCourses = deptCourses.length ? deptCourses : faker.helpers.arrayElements(courses, randInt(2, 4));
      const nLogins = randInt(2, 5) * SINCE_WEEKS;
      for (let i = 0; i < nLogins; i++) {
        const t = randDateBetween(windowStart, windowEnd);
        await enqueue(
          'activity_events',
          eventShell(t, u.user_id, idem('login', u.user_id, t.toISOString()), {
            event_id: faker.string.uuid(),
            event_type: 'login',
            course_id: myCourses[0] ? myCourses[0].id : null,
            module_id: null,
            material_id: null,
            session_id: faker.string.uuid(),
            duration_sec: null,
            properties: { channel: 'simulation', role: 'lecturer' },
          }),
        );
      }
      for (const c of myCourses) {
        const pages = randInt(3, 12);
        for (let p = 0; p < pages; p++) {
          const t = randDateBetween(windowStart, windowEnd);
          await enqueue(
            'activity_events',
            eventShell(t, u.user_id, idem('page_view', u.user_id, c.id, p, t.toISOString()), {
              event_id: faker.string.uuid(),
              event_type: 'page_view',
              course_id: c.id,
              properties: { page: `/courses/${c.id}/roster`, device: 'web' },
            }),
          );
        }
        const sessions = sessionDatesForCourse(c, 6);
        for (const sd of sessions) {
          if (Math.random() > 0.85) continue;
          const t = new Date(sd.getTime() + randBetween(0, 2) * 3600 * 1000);
          await enqueue(
            'activity_events',
            eventShell(t, u.user_id, idem('session_heartbeat', u.user_id, c.id, sd.toISOString()), {
              event_id: faker.string.uuid(),
              event_type: 'session_heartbeat',
              course_id: c.id,
              duration_sec: randInt(300, 3600),
              properties: { room: c.class_room || 'A101' },
            }),
          );
        }
      }
      continue;
    }

    const persona = u.persona;
    const deptCourses = coursesForDepartment(u.department_id, courses);
    const enrolled =
      deptCourses.length >= 3
        ? faker.helpers.arrayElements(deptCourses, randInt(3, Math.min(6, deptCourses.length)))
        : faker.helpers.arrayElements(courses, randInt(2, Math.min(5, courses.length)));

    const nLogin = loginSessionsCount(persona);
    for (let i = 0; i < nLogin; i++) {
      const t = randDateBetween(windowStart, windowEnd);
      await enqueue(
        'activity_events',
        eventShell(t, u.user_id, idem('login', u.user_id, i, t.toISOString()), {
          event_id: faker.string.uuid(),
          event_type: 'login',
          course_id: enrolled[0] ? enrolled[0].id : null,
          session_id: faker.string.uuid(),
          duration_sec: null,
          properties: { persona },
        }),
      );
    }

    if (persona === 'dormant' && Math.random() < 0.4) {
      continue;
    }

    for (const c of enrolled) {
      const pv = pageViewsPerCourse(persona);
      for (let p = 0; p < pv; p++) {
        const t = randDateBetween(windowStart, windowEnd);
        const tab = faker.helpers.arrayElement(['overview', 'assignments', 'attendance', 'materials']);
        await enqueue(
          'activity_events',
          eventShell(t, u.user_id, idem('page_view', u.user_id, c.id, tab, p, t.toISOString()), {
            event_id: faker.string.uuid(),
            event_type: 'page_view',
            course_id: c.id,
            properties: { page: `/courses/${c.id}/${tab}`, persona },
          }),
        );
      }

      const maxSess = persona === 'sparse' ? 3 : persona === 'dormant' ? 2 : 12;
      const sessions = sessionDatesForCourse(c, maxSess);
      const attendCap =
        persona === 'star' || persona === 'steady'
          ? sessions.length
          : persona === 'dormant'
            ? Math.min(1, sessions.length)
            : Math.max(1, Math.floor(sessions.length * (persona === 'struggling' ? 0.45 : 0.65)));

      let attended = 0;
      for (const sd of sessions) {
        if (attended >= attendCap) break;
        if (persona === 'sparse' && Math.random() > 0.35) continue;
        const status = attendanceRoll(persona);
        const t = new Date(sd.getTime() + randBetween(0, 1.5) * 3600 * 1000);
        await enqueue(
          'attendance_events',
          eventShell(t, u.user_id, idem('attendance', u.user_id, c.id, sd.toISOString(), status), {
            event_id: faker.string.uuid(),
            event_type: status === 'Absent' ? 'session_absent' : 'session_attended',
            course_id: c.id,
            status,
            notes: null,
            properties: { session_date: sd.toISOString(), persona },
          }),
        );
        attended++;
      }

      const assignCount =
        persona === 'dormant' ? randInt(0, 1) : persona === 'sparse' ? randInt(0, 2) : randInt(1, 4);
      for (let a = 0; a < assignCount; a++) {
        assignmentSeq += 1;
        const dueAt = randDateBetween(windowStart, windowEnd);
        const submittedAt = submissionTimeForPersona(persona, dueAt);
        const label = timingLabel(submittedAt, dueAt);
        if (persona === 'dormant' && Math.random() < 0.55) continue;

        const submissionId = 2000000 + assignmentSeq;
        const score =
          persona === 'star'
            ? randBetween(8, 10)
            : persona === 'struggling'
              ? randBetween(3, 6.5)
              : randBetween(5.5, 8.5);

        await enqueue(
          'assessment_events',
          eventShell(submittedAt, u.user_id, idem('submission', u.user_id, assignmentSeq, submittedAt.toISOString()), {
            event_id: faker.string.uuid(),
            event_type: 'submission_created',
            course_id: c.id,
            assignment_id: assignmentSeq,
            submission_id: submissionId,
            timing_label: label,
            final_score: Math.random() < 0.15 ? null : Number(score.toFixed(2)),
            properties: {
              due_at: dueAt.toISOString(),
              persona,
              course_code: c.course_code,
            },
          }),
        );

        if (label !== 'late' || Math.random() < 0.85) {
          const gradedAt = new Date(submittedAt.getTime() + randBetween(6, 96) * 3600 * 1000);
          const grader = users.filter((x) => x.role_id === 2 && x.department_id === c.from_department);
          const marker = grader.length ? faker.helpers.arrayElement(grader) : faker.helpers.arrayElement(users.filter((x) => x.role_id === 2));
          if (marker) {
            await enqueue(
              'assessment_events',
              eventShell(gradedAt, marker.user_id, idem('graded', submissionId, gradedAt.toISOString()), {
                event_id: faker.string.uuid(),
                event_type: 'graded',
                course_id: c.id,
                assignment_id: assignmentSeq,
                submission_id: submissionId,
                timing_label: label,
                final_score: Number(score.toFixed(2)),
                properties: { student_id: u.user_id },
              }),
            );
          }
        }
      }

      if (persona === 'star' && Math.random() < 0.35) {
        const t = randDateBetween(windowStart, windowEnd);
        await enqueue(
          'activity_events',
          eventShell(t, u.user_id, idem('heartbeat', u.user_id, c.id, t.toISOString()), {
            event_id: faker.string.uuid(),
            event_type: 'session_heartbeat',
            course_id: c.id,
            duration_sec: randInt(120, 900),
            properties: { persona },
          }),
        );
      }
    }
  }

  await flushBuffers(mongoDb, buffers, stats);

  console.log('Done. Total documents written (approx):', stats.written);
  if (mongoClient) await mongoClient.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
