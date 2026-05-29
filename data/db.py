"""db.py — WorkspaceDB: DuckDB connection + schema management."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

_DEFAULT_DB_NAME = "sdp.db"

# Full DDL — all CREATE TABLE IF NOT EXISTS statements (idempotent)
_SCHEMA_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id   INTEGER PRIMARY KEY,
        sdp_name      TEXT        NOT NULL,
        run_name      TEXT        NOT NULL,
        snapshot_dir  TEXT        NOT NULL,
        ingested_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draw_calls (
        snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
        api_id        INTEGER NOT NULL,
        dc_id         INTEGER NOT NULL,
        api_name      TEXT    NOT NULL DEFAULT '',
        pipeline_id   INTEGER,
        vertex_count  INTEGER NOT NULL DEFAULT 0,
        index_count   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shader_stages (
        snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
        pipeline_id   INTEGER NOT NULL,
        stage         TEXT    NOT NULL,
        module_id     INTEGER,
        entry_point   TEXT    NOT NULL DEFAULT '',
        file_path     TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (snapshot_id, pipeline_id, stage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dc_shader_stages (
        snapshot_id   INTEGER NOT NULL,
        api_id        INTEGER NOT NULL,
        pipeline_id   INTEGER NOT NULL,
        stage         TEXT    NOT NULL,
        PRIMARY KEY (snapshot_id, api_id, pipeline_id, stage),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS textures (
        snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
        texture_id    INTEGER NOT NULL,
        width INTEGER, height INTEGER, depth INTEGER,
        format TEXT, layers INTEGER, levels INTEGER,
        file_path TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (snapshot_id, texture_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dc_textures (
        snapshot_id INTEGER NOT NULL,
        api_id      INTEGER NOT NULL,
        texture_id  INTEGER NOT NULL,
        PRIMARY KEY (snapshot_id, api_id, texture_id),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meshes (
        snapshot_id INTEGER NOT NULL,
        api_id      INTEGER NOT NULL,
        mesh_file   TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (snapshot_id, api_id),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dc_render_targets (
        snapshot_id      INTEGER NOT NULL,
        api_id           INTEGER NOT NULL,
        attachment_index INTEGER NOT NULL,
        attachment_type  TEXT,
        resource_id      INTEGER,
        renderpass_id    INTEGER,
        framebuffer_id   INTEGER,
        width            INTEGER,
        height           INTEGER,
        format           TEXT,
        PRIMARY KEY (snapshot_id, api_id, attachment_index),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS setpass (
        snapshot_id   INTEGER NOT NULL,
        api_id        INTEGER NOT NULL,
        call_id       INTEGER NOT NULL,
        call_name     TEXT    NOT NULL DEFAULT '',
        parameters    TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (snapshot_id, api_id, call_id),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS labels (
        snapshot_id     INTEGER     NOT NULL,
        api_id          INTEGER     NOT NULL,
        category        TEXT        NOT NULL DEFAULT '',
        subcategory     TEXT        NOT NULL DEFAULT '',
        detail          TEXT        NOT NULL DEFAULT '',
        reason_tags     TEXT        NOT NULL DEFAULT '[]',
        confidence      DOUBLE      NOT NULL DEFAULT 0.0,
        label_source    TEXT        NOT NULL DEFAULT 'rule',
        bottleneck_text TEXT,
        embedding       FLOAT[],
        labeled_at      TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (snapshot_id, api_id),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics (
        snapshot_id                   INTEGER NOT NULL,
        api_id                        INTEGER NOT NULL,
        -- Misc
        clocks                        BIGINT,
        preemptions                   BIGINT,
        avg_preemption_delay          DOUBLE,
        -- Memory Bandwidth
        read_total_bytes              BIGINT,
        write_total_bytes             BIGINT,
        tex_mem_read_bytes            BIGINT,
        vertex_mem_read_bytes         BIGINT,
        sp_mem_read_bytes             BIGINT,
        avg_bytes_per_fragment        DOUBLE,
        avg_bytes_per_vertex          DOUBLE,
        -- Geometry
        fragments_shaded              BIGINT,
        vertices_shaded               BIGINT,
        reused_vertices               BIGINT,
        pre_clipped_polygons          BIGINT,
        lrz_pixels_killed             BIGINT,
        avg_polygon_area              DOUBLE,
        avg_vertices_per_polygon      DOUBLE,
        prims_clipped_pct             DOUBLE,
        prims_trivially_rejected_pct  DOUBLE,
        -- Texture
        tex_fetch_stall_pct           DOUBLE,
        tex_l1_miss_pct               DOUBLE,
        tex_l2_miss_pct               DOUBLE,
        tex_pipes_busy_pct            DOUBLE,
        linear_filtered_pct           DOUBLE,
        nearest_filtered_pct          DOUBLE,
        anisotropic_filtered_pct      DOUBLE,
        non_base_level_tex_pct        DOUBLE,
        l1_tex_cache_miss_per_pixel   DOUBLE,
        textures_per_fragment         DOUBLE,
        textures_per_vertex           DOUBLE,
        -- Shader / ALU
        shaders_busy_pct              DOUBLE,
        shaders_stalled_pct           DOUBLE,
        time_alus_working_pct         DOUBLE,
        time_efus_working_pct         DOUBLE,
        time_shading_vertices_pct     DOUBLE,
        time_shading_fragments_pct    DOUBLE,
        time_compute_pct              DOUBLE,
        shader_alu_capacity_pct       DOUBLE,
        wave_context_occupancy_pct    DOUBLE,
        instruction_cache_miss_pct    DOUBLE,
        fragment_instructions         BIGINT,
        fragment_alu_instr_full       BIGINT,
        fragment_alu_instr_half       BIGINT,
        fragment_efu_instructions     BIGINT,
        vertex_instructions           BIGINT,
        alu_per_fragment              DOUBLE,
        alu_per_vertex                DOUBLE,
        efu_per_fragment              DOUBLE,
        efu_per_vertex                DOUBLE,
        -- Vertex Fetch / Stall
        vertex_fetch_stall_pct        DOUBLE,
        stalled_on_system_mem_pct     DOUBLE,
        PRIMARY KEY (snapshot_id, api_id),
        FOREIGN KEY (snapshot_id, api_id) REFERENCES draw_calls(snapshot_id, api_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_stats (
        snapshot_id  INTEGER     NOT NULL REFERENCES snapshots(snapshot_id),
        category     TEXT        NOT NULL,
        dc_count     INTEGER     NOT NULL,
        clocks_sum   BIGINT      NOT NULL,
        clocks_pct   DOUBLE,
        avg_conf     DOUBLE,
        computed_at  TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (snapshot_id, category)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS questions (
        id           TEXT    PRIMARY KEY,
        title        TEXT    NOT NULL,
        model_name   TEXT    NOT NULL,
        model_params TEXT    NOT NULL DEFAULT '{}',
        viz_type     TEXT    NOT NULL,
        viz_config   TEXT    NOT NULL DEFAULT '{}',
        is_builtin   BOOLEAN NOT NULL DEFAULT false,
        created_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboards (
        id           TEXT    PRIMARY KEY,
        title        TEXT    NOT NULL,
        question_ids TEXT    NOT NULL DEFAULT '[]',
        created_at   TIMESTAMPTZ NOT NULL,
        updated_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_descriptions (
        snapshot_id       INTEGER PRIMARY KEY REFERENCES snapshots(snapshot_id),
        screenshot_path   TEXT        NOT NULL DEFAULT '',
        screenshot_width  INTEGER,
        screenshot_height INTEGER,
        screenshot_size   BIGINT,
        description       TEXT,
        vlm_model         TEXT,
        status            TEXT        NOT NULL DEFAULT 'pending',
        error_msg         TEXT,
        generated_at      TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sdp_files (
        path              TEXT PRIMARY KEY,
        name              TEXT        NOT NULL,
        size              BIGINT      NOT NULL DEFAULT 0,
        modified          DOUBLE      NOT NULL DEFAULT 0,
        app               TEXT,
        activity          TEXT,
        device_model      TEXT,
        device_manufacturer TEXT,
        device_platform   TEXT,
        gpu_renderer      TEXT,
        api               TEXT,
        capture_time      TIMESTAMPTZ,
        snapshot_count    INTEGER     NOT NULL DEFAULT 0,
        analysis_dir      TEXT,
        scan_dir          TEXT,
        ingested_at       TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id              TEXT PRIMARY KEY,
        name            TEXT        NOT NULL UNIQUE,
        description     TEXT        NOT NULL DEFAULT '',
        color           TEXT        NOT NULL DEFAULT '#6366f1',
        created_at      TIMESTAMPTZ NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS versions (
        id              TEXT PRIMARY KEY,
        project_id      TEXT        NOT NULL REFERENCES projects(id),
        name            TEXT        NOT NULL,
        description     TEXT        NOT NULL DEFAULT '',
        ordinal         INTEGER     NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL,
        UNIQUE (project_id, name)
    )
    """,
]


class WorkspaceDB:
    """Global singleton managing the DuckDB connection."""

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            env = os.environ.get("SDP_DB_PATH")
            if env:
                db_path = Path(env)
            else:
                db_path = Path(__file__).parent / _DEFAULT_DB_NAME
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._path))
        self.ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    def conn(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """Return a new cursor on the shared connection — independent result set per caller."""
        return self._conn.cursor()

    def ensure_schema(self) -> None:
        """Run full DDL — each CREATE TABLE IF NOT EXISTS is idempotent."""
        for stmt in _SCHEMA_STMTS:
            self._conn.execute(stmt)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema without dropping existing data."""
        new_cols = [
            # snapshots — original C# snapshot index within the SDP session
            ("snapshots", "snap_index", "INTEGER"),
            ("snapshots", "capture_time", "TIMESTAMPTZ"),
            # meshes — geometry stats from OBJ
            ("meshes", "vertex_count", "INTEGER"),
            ("meshes", "face_count",   "INTEGER"),
            ("meshes", "normal_count", "INTEGER"),
            ("meshes", "uv_count",     "INTEGER"),
            ("meshes", "bbox_min",     "DOUBLE[]"),
            ("meshes", "bbox_max",     "DOUBLE[]"),
            # draw_calls — extended params
            ("draw_calls", "parameters",                  "TEXT DEFAULT ''"),
            ("draw_calls", "instance_count",              "INTEGER DEFAULT 0"),
            ("draw_calls", "first_vertex",                "INTEGER DEFAULT 0"),
            ("draw_calls", "first_index",                 "INTEGER DEFAULT 0"),
            ("draw_calls", "vertex_offset",               "INTEGER DEFAULT 0"),
            ("draw_calls", "first_instance",              "INTEGER DEFAULT 0"),
            ("draw_calls", "draw_count",                  "INTEGER DEFAULT 0"),
            ("draw_calls", "group_count_x",               "INTEGER DEFAULT 0"),
            ("draw_calls", "group_count_y",               "INTEGER DEFAULT 0"),
            ("draw_calls", "group_count_z",               "INTEGER DEFAULT 0"),
            # metrics — full counter set from DrawCallModels.cs CounterToKey
            ("metrics",    "preemptions",                 "BIGINT"),
            ("metrics",    "avg_preemption_delay",        "DOUBLE"),
            ("metrics",    "tex_mem_read_bytes",          "BIGINT"),
            ("metrics",    "vertex_mem_read_bytes",       "BIGINT"),
            ("metrics",    "sp_mem_read_bytes",           "BIGINT"),
            ("metrics",    "avg_bytes_per_fragment",      "DOUBLE"),
            ("metrics",    "avg_bytes_per_vertex",        "DOUBLE"),
            ("metrics",    "reused_vertices",             "BIGINT"),
            ("metrics",    "pre_clipped_polygons",        "BIGINT"),
            ("metrics",    "lrz_pixels_killed",           "BIGINT"),
            ("metrics",    "avg_polygon_area",            "DOUBLE"),
            ("metrics",    "avg_vertices_per_polygon",    "DOUBLE"),
            ("metrics",    "prims_clipped_pct",           "DOUBLE"),
            ("metrics",    "prims_trivially_rejected_pct","DOUBLE"),
            ("metrics",    "linear_filtered_pct",         "DOUBLE"),
            ("metrics",    "nearest_filtered_pct",        "DOUBLE"),
            ("metrics",    "anisotropic_filtered_pct",    "DOUBLE"),
            ("metrics",    "non_base_level_tex_pct",      "DOUBLE"),
            ("metrics",    "l1_tex_cache_miss_per_pixel", "DOUBLE"),
            ("metrics",    "textures_per_fragment",       "DOUBLE"),
            ("metrics",    "textures_per_vertex",         "DOUBLE"),
            ("metrics",    "time_efus_working_pct",       "DOUBLE"),
            ("metrics",    "time_shading_vertices_pct",   "DOUBLE"),
            ("metrics",    "time_shading_fragments_pct",  "DOUBLE"),
            ("metrics",    "time_compute_pct",            "DOUBLE"),
            ("metrics",    "shader_alu_capacity_pct",     "DOUBLE"),
            ("metrics",    "wave_context_occupancy_pct",  "DOUBLE"),
            ("metrics",    "instruction_cache_miss_pct",  "DOUBLE"),
            ("metrics",    "fragment_instructions",       "BIGINT"),
            ("metrics",    "fragment_alu_instr_full",     "BIGINT"),
            ("metrics",    "fragment_alu_instr_half",     "BIGINT"),
            ("metrics",    "fragment_efu_instructions",   "BIGINT"),
            ("metrics",    "vertex_instructions",         "BIGINT"),
            ("metrics",    "alu_per_fragment",            "DOUBLE"),
            ("metrics",    "alu_per_vertex",              "DOUBLE"),
            ("metrics",    "efu_per_fragment",            "DOUBLE"),
            ("metrics",    "efu_per_vertex",              "DOUBLE"),
            ("metrics",    "vertex_fetch_stall_pct",      "DOUBLE"),
            ("metrics",    "stalled_on_system_mem_pct",   "DOUBLE"),
            # snapshots — project & version
            ("snapshots",  "project_id",                  "TEXT"),
            ("snapshots",  "version_id",                  "TEXT"),
            # sdp_files — project & version (denormalized for fast filtering)
            ("sdp_files",  "project_id",                  "TEXT"),
            ("sdp_files",  "version_id",                  "TEXT"),
            ("sdp_files",  "label",                       "TEXT"),
            ("sdp_files",  "thumbnail",                   "TEXT"),
        ]
        existing = {
            (row[0], row[1])
            for row in self._conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns"
            ).fetchall()
        }
        for table, col, typedef in new_cols:
            if (table, col) not in existing:
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
        self._migrate_default_project()

    def _migrate_default_project(self) -> None:
        """Fill NULL project_id/version_id with Default project (idempotent)."""
        row = self._conn.execute(
            "SELECT id FROM projects WHERE name = 'Default' LIMIT 1"
        ).fetchone()
        if not row:
            return
        pid = row[0]
        ver = self._conn.execute(
            "SELECT id FROM versions WHERE project_id = ? AND name = 'unversioned' LIMIT 1",
            (pid,)
        ).fetchone()
        if not ver:
            return
        vid = ver[0]
        self._conn.execute(
            "UPDATE snapshots SET project_id = ?, version_id = ? WHERE project_id IS NULL",
            (pid, vid)
        )
        self._conn.execute(
            "UPDATE sdp_files SET project_id = ?, version_id = ? WHERE project_id IS NULL",
            (pid, vid)
        )

    def close(self) -> None:
        self._conn.close()
