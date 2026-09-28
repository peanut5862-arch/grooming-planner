-- Mobile Grooming Planner v12
-- Run this in Supabase -> SQL Editor -> New query.

create extension if not exists pgcrypto;

create table if not exists public.dogs (
    id uuid primary key default gen_random_uuid(),
    owner text not null,
    dog text not null,
    household_id text not null,
    phone text,
    area text,
    groomer text,
    last_groom date,
    frequency_weeks numeric,
    price numeric,
    minutes numeric,
    household_override_minutes numeric,
    address text,
    city text,
    state text,
    zip text,
    latitude double precision,
    longitude double precision,
    last_contacted date,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.appointments (
    id uuid primary key default gen_random_uuid(),
    date date not null,
    start_time text,
    end_time text,
    client text,
    area text,
    groomer text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists dogs_household_idx on public.dogs(household_id);
create index if not exists dogs_groomer_idx on public.dogs(groomer);
create index if not exists dogs_area_idx on public.dogs(area);
create index if not exists appointments_date_idx on public.appointments(date);

-- The Streamlit app uses a server-side service-role key stored in Streamlit Secrets.
-- Keep that key private. Do not commit it to GitHub.
