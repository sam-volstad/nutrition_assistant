class TargetRepository:
    def __init__(self, connection):
        self.con = connection

    def get_profile(self, profile_name: str):
        profile = self.con.execute(
            "SELECT profile_id FROM target_profiles WHERE profile_name = ?",
            [profile_name],
        ).fetchone()

        if profile is None:
            raise ValueError(f"Unknown target profile: {profile_name}")

        return self.con.execute(
            """
            SELECT
                nt.nutrient_id,
                n.name,
                n.unit_name,
                nt.minimum_amount,
                nt.target_amount,
                nt.maximum_amount,
                nt.reference_type,
                nt.notes
            FROM nutrient_targets_v2 nt
            JOIN nutrient n
                ON nt.nutrient_id = n.id
            WHERE nt.profile_id = ?
              AND nt.period = 'daily'
            ORDER BY n.rank
            """,
            [profile[0]],
        ).fetchdf()

    def list_profiles(self):
        return self.con.execute(
            """
            SELECT profile_name, notes
            FROM target_profiles
            ORDER BY profile_name
            """
        ).fetchdf()
