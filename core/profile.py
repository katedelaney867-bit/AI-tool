# -*- coding: utf-8 -*-
"""注册资料生成：显示名 + 生日。

提炼自 turb-gpt-free-register 的 core/name_samples.py 与 core/profile_utils.py。

显示名只生成英文字母和空格 —— OpenAI 会以 name_invalid_chars 拒绝其它字符。
生日在 18~65 岁之间随机，兼容「about-you」页面的年龄输入与年月日控件。
"""
from __future__ import annotations

import random
from datetime import date, timedelta

FIRST_NAMES = [
    "James", "Robert", "John", "Michael", "David", "William", "Richard", "Joseph",
    "Thomas", "Christopher", "Charles", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian",
    "George", "Timothy", "Ronald", "Jason", "Edward", "Jeffrey", "Ryan", "Jacob",
    "Gary", "Nicholas", "Eric", "Jonathan", "Stephen", "Larry", "Justin", "Scott",
    "Brandon", "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick", "Frank",
    "Raymond", "Jack", "Dennis", "Jerry", "Tyler", "Aaron", "Jose", "Adam",
    "Nathan", "Henry", "Douglas", "Zachary", "Peter", "Kyle", "Ethan", "Walter",
    "Noah", "Jeremy", "Christian", "Keith", "Roger", "Terry", "Gerald", "Harold",
    "Sean", "Austin", "Carl", "Arthur", "Lawrence", "Dylan", "Jesse", "Jordan",
    "Bryan", "Billy", "Bruce", "Albert", "Willie", "Gabriel", "Logan", "Alan",
    "Juan", "Wayne", "Roy", "Ralph", "Randy", "Eugene", "Vincent", "Russell",
    "Elijah", "Louis", "Bobby", "Philip", "Johnny", "Liam", "Mason", "Lucas",
    "Oliver", "Caleb", "Owen", "Isaac", "Carter", "Julian", "Levi", "Wyatt",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda",
    "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia",
    "Dorothy", "Amy", "Angela", "Shirley", "Anna", "Brenda", "Pamela", "Emma",
    "Nicole", "Helen", "Samantha", "Katherine", "Christine", "Debra", "Rachel",
    "Catherine", "Carolyn", "Janet", "Ruth", "Maria", "Heather", "Diane",
    "Virginia", "Julie", "Joyce", "Victoria", "Olivia", "Kelly", "Christina",
    "Lauren", "Joan", "Evelyn", "Judith", "Megan", "Cheryl", "Andrea", "Hannah",
    "Jacqueline", "Martha", "Gloria", "Teresa", "Ann", "Sara", "Madison",
    "Frances", "Kathryn", "Janice", "Jean", "Abigail", "Alice", "Julia", "Judy",
    "Sophia", "Grace", "Isabella", "Ava", "Mia", "Charlotte", "Amelia", "Harper",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
    "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner",
    "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris",
    "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan",
    "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim",
    "Cox", "Ward", "Richardson", "Watson", "Brooks", "Chavez", "Wood", "James",
    "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez",
    "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster", "Jimenez",
    "Powell", "Jenkins", "Perry", "Russell", "Sullivan", "Bell", "Coleman",
    "Butler", "Henderson", "Barnes", "Gonzales", "Fisher", "Vasquez", "Simmons",
    "Romero", "Patterson", "Alexander", "Hamilton", "Graham", "Reynolds",
    "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes", "Bryant", "Herrera",
    "Gibson", "Ellis", "Tran", "Medina", "Aguilar", "Stevens", "Murray", "Ford",
    "Castro", "Marshall", "Owens", "Harrison", "Fernandez", "Mcdonald", "Woods",
    "Washington", "Kennedy", "Wells", "Vargas", "Henry", "Chen", "Freeman",
    "Webb", "Tucker", "Guzman", "Burns", "Crawford", "Olson", "Simpson", "Porter",
    "Hunter", "Gordon", "Mendez", "Silva", "Shaw", "Snyder", "Dixon",
    "Munoz", "Hunt", "Hicks", "Holmes", "Palmer", "Wagner", "Black", "Robertson",
]

MIDDLE_NAMES = [
    "Lee", "Ray", "Jay", "Dean", "Cole", "Blake", "Grant", "Neil", "Ryan", "Evan",
    "Anne", "Marie", "Jane", "Rose", "Lynn", "Grace", "Claire", "Hope", "May", "Rae",
]


def random_display_name() -> str:
    """生成英文显示名；少量返回 First Middle Last，只含字母和空格。"""
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    if random.random() < 0.12:
        return f"{first} {random.choice(MIDDLE_NAMES)} {last}"
    return f"{first} {last}"


def _shift_year_safe(day: date, years: int) -> date:
    """按年偏移；2 月 29 日落到非闰年时回退到 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def random_birthday(min_age: int = 18, max_age: int = 65) -> str:
    """生成 [min_age, max_age] 岁之间的随机生日，格式 YYYY-MM-DD。"""
    if min_age < 0 or max_age < min_age:
        raise ValueError(f"年龄范围无效: min_age={min_age}, max_age={max_age}")
    today = date.today()
    oldest = _shift_year_safe(today, -max_age)
    youngest = _shift_year_safe(today, -min_age)
    span_days = (youngest - oldest).days
    return (oldest + timedelta(days=random.randint(0, span_days))).isoformat()
