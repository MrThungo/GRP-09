"""Landing-page team members and administrator-managed picture filenames."""
from pathlib import Path


TEAM_MEMBER_SPECS = (
    {
        "name": "Mncina Nomhle",
        "student_number": "224497847",
        "role": "Lab Manager",
        "linkedin_url": "https://linkedin.com/in/nomhle-mncina-b192833a0",
        "picture_position": "50% 18%",
    },
    {
        "name": "Papama Xuza",
        "student_number": "224153498",
        "role": "Doctor",
        "linkedin_url": (
            "https://www.linkedin.com/in/papama-xuza-70846b2b3"
            "?utm_source=share&utm_campaign=share_via"
            "&utm_content=profile&utm_medium=android_app"
        ),
        "picture_position": "50% 10%",
    },
    {
        "name": "Anam Thembani",
        "student_number": "219598274",
        "role": "Lab Technician",
        "linkedin_url": (
            "https://www.linkedin.com/in/anam-thembani-760488351"
            "?trk=contact-info"
        ),
        "picture_position": "50% 28%",
    },
    {
        "name": "Ndumiso Thungo",
        "student_number": "221411046",
        "role": "Admin & Patient",
        "linkedin_url": "https://www.linkedin.com/in/ndumiso-thungo-254470164/",
        "picture_position": "50% 14%",
    },
)


def landing_team_picture_filename(student_number):
    safe_number = "".join(
        character for character in str(student_number or "") if character.isdigit()
    )
    return f"landing-team-{safe_number}.jpg"


def landing_team_members(upload_directory):
    upload_directory = Path(upload_directory)
    members = []
    for spec in TEAM_MEMBER_SPECS:
        filename = landing_team_picture_filename(spec["student_number"])
        picture_path = upload_directory / filename
        initials = "".join(
            part[0].upper() for part in spec["name"].split()[:2] if part
        ) or "?"
        members.append(
            {
                **spec,
                "initials": initials,
                "picture_filename": filename if picture_path.is_file() else "",
            }
        )
    return members
