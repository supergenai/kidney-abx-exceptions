-- Synthetic kidney-care antibiotic exception dataset. NO PHI.
-- Idempotent: CREATE IF NOT EXISTS. generate.py truncates + reloads; pass --reset-schema to drop/recreate.

CREATE TABLE IF NOT EXISTS patients (
    patient_id        text PRIMARY KEY,
    mrn               text NOT NULL UNIQUE,
    age               integer NOT NULL CHECK (age BETWEEN 0 AND 120),
    sex               text NOT NULL CHECK (sex IN ('M', 'F')),
    weight_kg         numeric(5,1) NOT NULL CHECK (weight_kg > 0),
    ckd_stage         text NOT NULL CHECK (ckd_stage IN ('3a', '3b', '4', '5', '5D')),
    dialysis_modality text NOT NULL CHECK (dialysis_modality IN ('none', 'HD', 'PD')),
    dialysis_days     text,
    clinic            text NOT NULL
);

CREATE TABLE IF NOT EXISTS labs (
    id           bigserial PRIMARY KEY,
    patient_id   text NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    test_code    text NOT NULL,
    test_name    text NOT NULL,
    value        numeric NOT NULL,
    unit         text NOT NULL,
    collected_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS labs_patient_idx ON labs (patient_id, test_code, collected_at);

CREATE TABLE IF NOT EXISTS allergies (
    id          bigserial PRIMARY KEY,
    patient_id  text NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    agent       text NOT NULL,
    reaction    text,
    severity    text NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe', 'anaphylaxis', 'unknown')),
    recorded_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS allergies_patient_idx ON allergies (patient_id);

CREATE TABLE IF NOT EXISTS microbiology (
    id             bigserial PRIMARY KEY,
    patient_id     text NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    specimen       text NOT NULL,
    collected_at   timestamptz NOT NULL,
    organism       text,
    antibiotic     text,
    interpretation text CHECK (interpretation IN ('S', 'I', 'R')),
    mic            text,
    -- a row with no organism is a negative culture: no susceptibility allowed
    CHECK (organism IS NOT NULL OR (antibiotic IS NULL AND interpretation IS NULL))
);
CREATE INDEX IF NOT EXISTS microbiology_patient_idx ON microbiology (patient_id, collected_at);

CREATE TABLE IF NOT EXISTS clinical_notes (
    id          bigserial PRIMARY KEY,
    patient_id  text NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    note_type   text NOT NULL,
    author_role text NOT NULL,
    written_at  timestamptz NOT NULL,
    body        text NOT NULL
);
CREATE INDEX IF NOT EXISTS clinical_notes_patient_idx ON clinical_notes (patient_id, written_at);

CREATE TABLE IF NOT EXISTS exception_requests (
    request_id    text PRIMARY KEY,
    patient_id    text NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    drug          text NOT NULL CHECK (drug IN ('vancomycin', 'meropenem', 'daptomycin')),
    dose_mg       numeric NOT NULL CHECK (dose_mg > 0),
    frequency     text NOT NULL,
    route         text NOT NULL CHECK (route IN ('IV', 'PO', 'IP')),
    indication    text NOT NULL,
    justification text NOT NULL,
    requested_at  timestamptz NOT NULL,
    prescriber    text NOT NULL
);
CREATE INDEX IF NOT EXISTS exception_requests_patient_idx ON exception_requests (patient_id);
